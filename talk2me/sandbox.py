"""Opt-in OS sandbox for the agent (--sandbox).

The denylist reads command lines, so a script walks past it. This doesn't read
anything: the kernel refuses the operation, whatever process asks. It wraps the
agent's whole process tree, so it covers Bash, scripts those commands run, and the
agent's own Write and Edit tools alike.

- macOS: sandbox-exec with a generated Seatbelt profile. Apple marks sandbox-exec
  deprecated, but it ships on every Mac and it's what the system's own tools use.
- Linux: bubblewrap (bwrap) with a read-only root and a private network namespace.
- Windows: not available, and --sandbox refuses to start rather than pretending.

What the agent can do inside: write to the project, a scratch temp dir, and the
agent's own session state. Reach the network only through AllowlistProxy, which
runs out here in talk2me and tunnels to named hosts on port 443. Nothing else, DNS
included. Read most of the disk, minus the usual secret stores.

Limits are spelled out in SECURITY.md. Short version: the allowlist includes the
model API, the project is writable by design, and on Linux the login token file
has to stay readable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# What the Claude CLI needs to reach: the model API and login refresh.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (".anthropic.com", ".claude.ai", "claude.ai", ".claude.com")

# Relative to $HOME. Hidden even from reading: the network rule stops most theft,
# and this covers whatever the allowlist can't.
SECRET_PATHS: tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc",
    ".git-credentials", ".config/gcloud", ".config/gh", ".zsh_history", ".bash_history",
    "Library/Keychains", "Library/Cookies", "Library/Messages", "Library/Mail",
    "Library/Safari", "Library/Application Support/Google/Chrome",
    "Library/Application Support/Firefox",
)

# Under $HOME, where the agent keeps session state.
AGENT_STATE_PATHS: tuple[str, ...] = (".claude", ".cache/claude", ".local/state/claude", ".config/claude")
AGENT_STATE_PREFIX = ".claude.json"  # plus its atomic-write siblings, .claude.json.tmp.*

# Inside the writable agent state and project, these change what runs LATER, outside
# the sandbox, so they stay read-only. A hook planted here would be a clean escape.
LOCKED_AGENT_FILES: tuple[str, ...] = (
    ".claude/settings.json", ".claude/settings.local.json", ".claude/CLAUDE.md", ".claude/hooks",
    ".claude/commands", ".claude/agents", ".claude/skills", ".claude/plugins",
)
LOCKED_PROJECT_FILES: tuple[str, ...] = (
    ".git/hooks", ".git/config", ".claude/settings.json", ".claude/settings.local.json",
    ".claude/hooks", ".claude/commands", ".claude/agents", ".claude/skills", ".mcp.json", ".envrc",
)

# macOS services that launch or reach things outside the sandbox, plus audio: an
# agent has no business near the sound system, and this keeps it that way.
_DENIED_MACH_REGEX = (
    r"^com\.apple\.(coreservices\.launchservicesd|lsd\.|pasteboard|audio\.|coreaudio|"
    r"windowserver|dock\.|dnssd\.|tccd|ScreenCapture|screencapture|systemevents)"
)
_DENIED_EXECS = (
    "/usr/bin/open", "/usr/bin/osascript", "/bin/launchctl", "/usr/bin/pbcopy", "/usr/bin/pbpaste",
    "/usr/bin/afplay", "/usr/bin/afrecord", "/usr/sbin/screencapture",
)


class SandboxUnavailable(RuntimeError):
    """--sandbox was asked for and can't be delivered. Never run without it."""


@dataclass
class SandboxSettings:
    project: str
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    allow_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_HOSTS))
    # Let the agent reach other services on this machine's loopback (macOS only;
    # on Linux the sandbox has its own private loopback, so dev servers just work).
    allow_localhost: bool = False


def host_allowed(host: str, port: int, allow_hosts: list[str]) -> bool:
    """Port 443 to a listed host. '.example.com' also covers its subdomains."""
    if port != 443:
        return False
    host = host.lower().rstrip(".")
    for rule in allow_hosts:
        rule = rule.lower()
        if host == rule.lstrip(".") or (rule.startswith(".") and host.endswith(rule)):
            return True
    return False


def host_of_url(url: str) -> str | None:
    return urlsplit(url).hostname


class AllowlistProxy:
    """The sandbox's one way out: a CONNECT proxy that says no to almost everything.

    It never sees inside the tunnel. It runs outside the sandbox, on loopback
    (macOS) or a Unix socket bound into the sandbox (Linux).
    """

    def __init__(self, allow_hosts: list[str], unix_path: str | None = None) -> None:
        self._allow = allow_hosts
        self._unix_path = unix_path
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.refused: list[str] = []

    async def start(self) -> None:
        if self._unix_path:
            self._server = await asyncio.start_unix_server(self._handle, path=self._unix_path)
        else:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 1)
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(parts) != 3 or parts[0] != "CONNECT":
                await self._refuse(writer, "405 Method Not Allowed", " ".join(parts[:2]))
                return
            host, _, port_text = parts[1].rpartition(":")
            host = host.strip("[]")
            port = int(port_text) if port_text.isdigit() else 0
            if not host_allowed(host, port, self._allow):
                await self._refuse(writer, "403 Forbidden", parts[1])
                return
            try:
                up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
            except (TimeoutError, OSError):
                await self._refuse(writer, "502 Bad Gateway", parts[1])
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _refuse(self, writer: asyncio.StreamWriter, status: str, what: str) -> None:
        self.refused.append(what)
        writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
        await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _quoted(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _regex_escape(path: str) -> str:
    out = []
    for ch in path:
        out.append("\\" + ch if ch in '.^$*+?()[]{}|\\"' else ch)
    return "".join(out)


def seatbelt_profile(settings: SandboxSettings, proxy_port: int, scratch: str) -> str:
    """The macOS policy. Seatbelt applies the LAST matching rule, so order matters."""
    home = _real(settings.home)
    project = _real(settings.project)
    lines = ["(version 1)", "(allow default)", "", "; Network: the proxy and nothing else."]
    lines += ["(deny network*)", f'(allow network-outbound (remote ip "localhost:{proxy_port}"))']
    if settings.allow_localhost:
        lines += ['(allow network-outbound (remote ip "localhost:*"))']
    lines += ['(allow network-bind (local ip "localhost:*"))', '(allow network-inbound (local ip "localhost:*"))']
    lines += ["(allow network-outbound (remote unix-socket (subpath " + _quoted(project) + ")))"]
    lines += ["", "; Writes: the project, scratch space and the agent's own state."]
    lines += ["(deny file-write*)"]
    lines += ['(allow file-write* (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/(ttys[0-9]+|fd/)"))']
    lines += [f"(allow file-write* (subpath {_quoted(project)}))"]
    lines += [f"(allow file-write* (subpath {_quoted(_real(scratch))}))"]
    for rel in AGENT_STATE_PATHS:
        lines += [f"(allow file-write* (subpath {_quoted(os.path.join(home, rel))}))"]
    lines += [f'(allow file-write* (regex #"^{_regex_escape(os.path.join(home, AGENT_STATE_PREFIX))}"))']
    lines += ["", "; Things that would run later, outside the sandbox."]
    for rel in LOCKED_AGENT_FILES:
        lines += [f"(deny file-write* (subpath {_quoted(os.path.join(home, rel))}))"]
    escaped = _regex_escape(project)
    for rel in LOCKED_PROJECT_FILES:
        # Anywhere in the tree: a nested repo's hooks fire just the same.
        lines += [f'(deny file-write* (regex #"^{escaped}(/.*)?/{_regex_escape(rel)}(/|$)"))']
    lines += ["", "; Secrets stay hidden."]
    for rel in SECRET_PATHS:
        lines += [f"(deny file-read* (subpath {_quoted(os.path.join(home, rel))}))"]
    lines += ["", "; No launching things outside the sandbox, and no audio."]
    lines += [f'(deny mach-lookup (global-name-regex #"{_DENIED_MACH_REGEX}"))', "(deny appleevent-send)"]
    for path in _DENIED_EXECS:
        lines += [f"(deny process-exec (literal {_quoted(path)}))"]
    return "\n".join(lines) + "\n"


def bwrap_argv(settings: SandboxSettings, socket_dir: str, port: int) -> list[str]:
    """The Linux policy, as a bwrap command line ending right before the program."""
    home = _real(settings.home)
    project = _real(settings.project)
    argv = [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--unshare-all", "--die-with-parent", "--new-session",
    ]
    argv += ["--bind", project, project]
    argv += ["--ro-bind", socket_dir, socket_dir]
    for rel in AGENT_STATE_PATHS:
        path = os.path.join(home, rel)
        if os.path.isdir(path):
            argv += ["--bind", path, path]
    state_file = os.path.join(home, AGENT_STATE_PREFIX)
    if os.path.exists(state_file):
        argv += ["--bind", state_file, state_file]
    for rel in LOCKED_AGENT_FILES:
        path = os.path.join(home, rel)
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    for rel in LOCKED_PROJECT_FILES:
        path = os.path.join(project, rel)
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    for rel in SECRET_PATHS:
        path = os.path.join(home, rel)
        if os.path.isdir(path):
            argv += ["--tmpfs", path]
        elif os.path.exists(path):
            argv += ["--ro-bind", "/dev/null", path]
    argv += ["--chdir", project, "--"]
    argv += [sys.executable, os.path.join(os.path.dirname(__file__), "sandbox_forward.py")]
    argv += [os.path.join(socket_dir, "proxy.sock"), str(port), "--"]
    return argv


def available() -> str | None:
    """None when --sandbox can run here, else a sentence saying why it can't."""
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec"):
            return None
        return "--sandbox needs sandbox-exec, which is missing from this Mac"
    if sys.platform.startswith("linux"):
        if not shutil.which("bwrap"):
            return "--sandbox needs bubblewrap on Linux: sudo apt-get install bubblewrap"
        probe = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--", "/bin/true"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0:
            return None
        return (
            f"--sandbox: bwrap can't run here ({probe.stderr.strip()[:120]}). On Ubuntu, "
            "AppArmor may be blocking it: sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0"
        )
    return f"--sandbox isn't available on {sys.platform}; there's no OS sandbox wired up for it"


# Linux's forwarder listens on this port inside the sandbox's private loopback.
_LINUX_PROXY_PORT = 18443


class Sandbox:
    """One sandboxed launch: owns the proxy and whatever temp files the policy needs."""

    def __init__(self, settings: SandboxSettings) -> None:
        self.settings = settings
        self._proxy: AllowlistProxy | None = None
        self._dir: tempfile.TemporaryDirectory | None = None
        self.scratch = ""

    async def start(self) -> None:
        reason = available()
        if reason:
            raise SandboxUnavailable(reason)
        self._dir = tempfile.TemporaryDirectory(prefix="t2m-sandbox-")
        self.scratch = os.path.join(self._dir.name, "tmp")
        os.mkdir(self.scratch)
        unix_path = None
        if sys.platform != "darwin":
            unix_path = os.path.join(self._dir.name, "proxy.sock")
        self._proxy = AllowlistProxy(self.settings.allow_hosts, unix_path=unix_path)
        await self._proxy.start()

    @property
    def proxy(self) -> AllowlistProxy:
        assert self._proxy is not None
        return self._proxy

    def wrap(self, argv: list[str]) -> list[str]:
        if sys.platform == "darwin":
            profile = seatbelt_profile(self.settings, self.proxy.port, self.scratch)
            return ["sandbox-exec", "-p", profile, *argv]
        return [*bwrap_argv(self.settings, self._dir.name, _LINUX_PROXY_PORT), *argv]

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        port = self.proxy.port if sys.platform == "darwin" else _LINUX_PROXY_PORT
        url = f"http://127.0.0.1:{port}"
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[key] = url
        # Temp files go in a private folder (macOS) or a private /tmp (Linux), so
        # nothing the agent drops there outlives the session or lands in shared /tmp.
        temp = self.scratch if sys.platform == "darwin" else "/tmp"
        for key in ("TMPDIR", "TEMP", "TMP", "CLAUDE_CODE_TMPDIR"):
            env[key] = temp
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
        if self.settings.allow_localhost or sys.platform != "darwin":
            env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
        return env

    async def close(self) -> None:
        if self._proxy is not None:
            await self._proxy.close()
        if self._dir is not None:
            with contextlib.suppress(OSError):
                self._dir.cleanup()
            self._dir = None

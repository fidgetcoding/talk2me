"""--sandbox against a hostile script, for real. No agent, no audio, no account.

The pure part (policy text, allowlist, flags) runs everywhere. The rest launches
an actual sandbox-exec or bwrap around an actual script that tries to do damage,
inside a throwaway fake $HOME, then checks from OUTSIDE that nothing happened.
Each attack also runs unsandboxed first, so a pass can't mean the attack was just
broken. Skipped where no OS sandbox can run (Windows, or Linux without bwrap).

Run:  python -m tests.test_sandbox
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from talk2me import sandbox
from talk2me.sandbox import (
    AllowlistProxy,
    Sandbox,
    SandboxSettings,
    bwrap_argv,
    host_allowed,
    seatbelt_profile,
)

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


# Runs under test. Every attack prints "attack: OK" when it worked, so the
# unsandboxed control and the sandboxed run are compared on the same output.
HOSTILE = r'''
import os, shutil, socket, subprocess, sys
home, project, proxy_port = sys.argv[1], sys.argv[2], int(sys.argv[3])
LABEL = "io.talk2me.sandboxtest"

def attempt(name, fn):
    try:
        fn()
        print(name + ": OK", flush=True)
    except Exception as exc:
        print(name + ": blocked " + type(exc).__name__, flush=True)

def write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write("pwned\n")

def read(path):
    with open(path) as fh:
        fh.read()

def connect_out():
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()

def resolve():
    socket.getaddrinfo("example.com", 443)

def proxy_tunnel(host):
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    reply = sock.recv(200)
    sock.close()
    if b" 200 " not in reply:
        raise PermissionError(reply[:40].decode())

def symlink_write():
    link = os.path.join(project, "innocent")
    os.symlink(os.path.join(home, ".profile"), link)
    with open(link, "a") as fh:
        fh.write("pwned\n")

def hardlink_write():
    link = os.path.join(project, "innocent-hard")
    os.link(os.path.join(home, ".bashrc"), link)
    with open(link, "a") as fh:
        fh.write("pwned\n")

def rename_away():
    os.rename(os.path.join(home, ".rename-me"), os.path.join(project, "renamed"))

def udp_out():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(b"x", ("1.1.1.1", 53))

def unix_out():
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(3)
    sock.connect(sys.argv[5])
    sock.close()

def inject_keys():
    import fcntl, termios
    with open("/dev/tty", "rb") as tty:
        fcntl.ioctl(tty, termios.TIOCSTI, b"x")

def env_token():
    if "SSH_AUTH_SOCK" not in os.environ and "GITHUB_TOKEN" not in os.environ:
        raise LookupError("scrubbed")

def run(*argv):
    subprocess.run(argv, check=True, capture_output=True, timeout=10)

attempt("write shell profile", lambda: write(os.path.join(home, ".zshrc")))
attempt("write launch agent", lambda: write(os.path.join(home, "Library/LaunchAgents/evil.plist")))
attempt("read ssh key", lambda: read(os.path.join(home, ".ssh/id_rsa")))
attempt("read aws creds", lambda: read(os.path.join(home, ".aws/credentials")))
attempt("plant agent hook", lambda: write(os.path.join(home, ".claude/settings.json")))
attempt("plant git hook", lambda: write(os.path.join(project, ".git/hooks/pre-commit")))
attempt("edit git config", lambda: write(os.path.join(project, ".git/config")))
attempt("plant project settings", lambda: write(os.path.join(project, ".claude/settings.json")))
attempt("write through symlink", symlink_write)
attempt("write through hard link", hardlink_write)
attempt("move a file out of home", rename_away)
attempt("direct connection", connect_out)
attempt("dns lookup", resolve)
attempt("tunnel to a stranger", lambda: proxy_tunnel("evil.example"))
attempt("tunnel with a sneaky host", lambda: proxy_tunnel("evil.example/.anthropic.com"))
attempt("plant an MCP server", lambda: write(os.path.join(home, ".claude.json")))
attempt("plant an agent rule", lambda: write(os.path.join(home, ".claude/rules/evil.md")))
attempt("plant a nested git hook", lambda: write(os.path.join(project, "sub/.git/hooks/pre-commit")))
attempt("plant an editor task", lambda: write(os.path.join(project, ".vscode/tasks.json")))
attempt("udp out", udp_out)
attempt("bash /dev/tcp", lambda: run("/bin/bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/443"))
attempt("system file", lambda: write("/etc/t2m-sandbox-test"))
attempt("kill a process outside", lambda: os.kill(int(sys.argv[4]), 15))
attempt("unix socket outside", unix_out)
attempt("inject keystrokes", inject_keys)
attempt("find a token in the environment", lambda: env_token())
if sys.platform == "darwin":
    attempt("spawn open", lambda: run("/usr/bin/open", "-g", "-a", "NoSuchAppT2M"))
    attempt("spawn osascript", lambda: run("/usr/bin/osascript", "-e", "return 1"))
    attempt("sudo", lambda: run("/usr/bin/sudo", "-n", "true"))
    attempt("crontab", lambda: run("/usr/bin/crontab", "-l"))
    attempt("at", lambda: run("/usr/bin/at", "-l"))
    attempt("launchctl submit", lambda: run("/bin/launchctl", "submit", "-l", LABEL, "--", "/usr/bin/touch", os.path.join(home, "launchd-escape")))
    def renamed(binary, *args):
        copy = os.path.join(project, "innocent-" + os.path.basename(binary))
        shutil.copyfile(binary, copy)
        os.chmod(copy, 0o755)
        run(copy, *args)
    attempt("renamed launchctl", lambda: renamed("/bin/launchctl", "submit", "-l", LABEL, "--", "/usr/bin/touch", os.path.join(home, "launchd-escape")))
    attempt("renamed open", lambda: renamed("/usr/bin/open", "-g", "-a", "NoSuchAppT2M"))

# Normal work has to keep working.
attempt("normal: write in project", lambda: write(os.path.join(project, "src/ok.txt")))
attempt("normal: write agent session", lambda: write(os.path.join(home, ".claude/projects/session.jsonl")))
attempt("normal: temp file", lambda: __import__("tempfile").NamedTemporaryFile().close())
attempt("normal: read project", lambda: read(os.path.join(project, "README")))
attempt("normal: run git", lambda: run("git", "-C", project, "status"))
attempt("normal: git commit", lambda: run("git", "-C", project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"))
'''

ATTACKS_THAT_MUST_FAIL = (
    "write shell profile", "write launch agent", "read ssh key", "read aws creds", "plant agent hook",
    "plant git hook", "edit git config", "plant project settings", "write through symlink",
    "write through hard link", "move a file out of home", "direct connection", "dns lookup",
    "tunnel to a stranger", "tunnel with a sneaky host", "plant an MCP server", "plant an agent rule",
    "udp out", "bash /dev/tcp", "system file",
    "kill a process outside", "inject keystrokes", "find a token in the environment",
)
MAC_ATTACKS = ("plant a nested git hook", "plant an editor task", "unix socket outside", "sudo", "crontab", "at", "spawn open", "spawn osascript", "launchctl submit", "renamed launchctl", "renamed open")
NORMAL_WORK = (
    "normal: write in project", "normal: write agent session", "normal: temp file",
    "normal: read project", "normal: run git", "normal: git commit",
)


def _fixture(root: Path) -> tuple[Path, Path, Path]:
    home, project = root / "home", root / "project"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/id_rsa").write_text("SECRET\n")
    (home / ".aws").mkdir()
    (home / ".aws/credentials").write_text("SECRET\n")
    (home / ".claude/projects").mkdir(parents=True)
    (home / ".claude/settings.json").write_text("{}\n")
    (home / ".profile").write_text("clean\n")
    (home / ".bashrc").write_text("clean\n")
    (home / ".rename-me").write_text("clean\n")
    (home / ".claude.json").write_text("{}\n")
    project.mkdir()
    (project / "README").write_text("hello\n")
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text("{}\n")
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / ".git/hooks").mkdir(exist_ok=True)
    script = root / "hostile.py"
    script.write_text(HOSTILE)
    return home, project, script


def _parse(output: str) -> dict[str, str]:
    return {
        line.split(":", 1)[0] if not line.startswith("normal:") else ":".join(line.split(":")[:2]):
        line.rsplit(":", 1)[-1].strip() if not line.startswith("normal:") else line.split(":")[2].strip()
        for line in output.splitlines()
        if ": " in line
    }


async def _proxy_for_control() -> AllowlistProxy:
    proxy = AllowlistProxy(["never.example"])
    await proxy.start()
    return proxy


async def _run_process(argv, env, cwd, *, new_session=False):
    # new_session matches how the backend launches a sandboxed agent.
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL, start_new_session=new_session,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 120)
    return proc.returncode, out.decode(errors="replace")


async def test_hostile_script() -> None:
    # /var/tmp, not the temp dir: the home must sit outside anything the sandbox
    # could be excused for allowing.
    with tempfile.TemporaryDirectory(dir="/var/tmp", prefix="t2m-sbx-test-") as tmp:
        root = Path(os.path.realpath(tmp))
        home, project, script = _fixture(root)
        box = Sandbox(SandboxSettings(project=str(project), home=str(home)))
        await box.start()
        try:
            port = box.proxy.port if sys.platform == "darwin" else sandbox._LINUX_PROXY_PORT
            victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            listener = socket.socket(socket.AF_UNIX)
            sock_path = str(root / "outside.sock")
            listener.bind(sock_path)
            listener.listen()
            argv = box.wrap([sys.executable, "-I", str(script), str(home), str(project), str(port), str(victim.pid), sock_path])
            base = {**os.environ, "SSH_AUTH_SOCK": "/tmp/agent.sock", "GITHUB_TOKEN": "SECRET", "ANTHROPIC_API_KEY": "kept"}
            env = box.env(base)
            env["HOME"] = str(home)
            check("the agent's own key survives the scrub", env.get("ANTHROPIC_API_KEY") == "kept")
            code, output = await _run_process(argv, env, str(project), new_session=True)
            check("the process outside survived", victim.poll() is None)
            victim.terminate()
            victim.wait(5)
            listener.close()
        finally:
            box_new = box.new_locked_paths()
            await box.close()
        print(output.rstrip())
        if sys.platform != "darwin":
            # Linux's documented limit: it can only lock what exists. It has to say so.
            check("Linux names the locked path the agent created", ".vscode" in box_new, str(box_new))
        result = _parse(output)
        check("hostile script ran to the end", code == 0 and "normal: run git" in result, f"exit {code}")
        attacks = ATTACKS_THAT_MUST_FAIL + (MAC_ATTACKS if sys.platform == "darwin" else ())
        for name in attacks:
            check(f"stopped: {name}", result.get(name, "missing").startswith("blocked"), result.get(name, "missing"))
        for name in NORMAL_WORK:
            check(f"still works: {name}", result.get(name) == "OK", result.get(name, "missing"))
        # From outside, because a script can lie about what it did.
        check("shell profile untouched", not (home / ".zshrc").exists())
        check("symlink target untouched", (home / ".profile").read_text() == "clean\n")
        check("hard link target untouched", (home / ".bashrc").read_text() == "clean\n")
        check("file not moved out", (home / ".rename-me").exists())
        check("agent settings untouched", (home / ".claude/settings.json").read_text() == "{}\n")
        check("MCP config untouched", (home / ".claude.json").read_text() == "{}\n")
        check("project settings untouched", (project / ".claude/settings.json").read_text() == "{}\n")
        check("git hook not planted", not (project / ".git/hooks/pre-commit").exists())
        check("launchd job never ran", not (home / "launchd-escape").exists())
        check("project write landed", (project / "src/ok.txt").exists())


async def test_control_run() -> None:
    """The same script with no sandbox: the attacks must succeed, or the real run
    proves nothing. Stays in the fake home, and leaves launchd and open alone."""
    with tempfile.TemporaryDirectory(dir="/var/tmp", prefix="t2m-sbx-ctl-") as tmp:
        root = Path(os.path.realpath(tmp))
        home, project, script = _fixture(root)
        source = script.read_text()
        # Everything from the macOS block on would reach the real system.
        script.write_text(
            source.replace('if sys.platform == "darwin":', "if False:")
            # With no sandbox this would really type into the terminal running the test.
            .replace('attempt("inject keystrokes", inject_keys)', "")
        )
        proxy = AllowlistProxy(["evil.example"])
        await proxy.start()
        env = {**os.environ, "HOME": str(home)}
        code, output = await _run_process(
            [sys.executable, "-I", str(script), str(home), str(project), str(proxy.port), "999999", "/nonexistent"], env, str(project)
        )
        await proxy.close()
        result = _parse(output)
        local = [a for a in (*ATTACKS_THAT_MUST_FAIL, "plant a nested git hook", "plant an editor task") if a in (
            "write shell profile", "write launch agent", "read ssh key", "read aws creds", "plant agent hook",
            "plant git hook", "edit git config", "plant project settings", "write through symlink",
            "write through hard link", "move a file out of home", "plant an MCP server", "plant an agent rule",
            "plant a nested git hook", "plant an editor task",
        )]
        worked = [a for a in local if result.get(a) == "OK"]
        check("control: file attacks work with no sandbox", worked == local, f"{len(worked)}/{len(local)}; exit {code}")


async def test_proxy_rules() -> None:
    proxy = AllowlistProxy(["allowed.test"])
    await proxy.start()
    try:
        async def ask(line: bytes) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(line)
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(100), 5)
            writer.close()
            return reply

        check("proxy refuses a stranger", b" 403 " in await ask(b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n"))
        check("proxy refuses other ports", b" 403 " in await ask(b"CONNECT allowed.test:22 HTTP/1.1\r\n\r\n"))
        check("proxy refuses plain GET", b" 405 " in await ask(b"GET http://allowed.test/ HTTP/1.1\r\n\r\n"))
        check("refusals are recorded", "evil.example:443" in proxy.refused)
    finally:
        await proxy.close()


def test_pure() -> None:
    allow = [".anthropic.com", "claude.ai"]
    check("subdomain allowed", host_allowed("api.anthropic.com", 443, allow))
    check("bare domain of a dot rule allowed", host_allowed("anthropic.com", 443, allow))
    check("lookalike refused", not host_allowed("evilanthropic.com", 443, allow))
    check("suffix trick refused", not host_allowed("anthropic.com.evil.test", 443, allow))
    check("exact rule covers no subdomains", not host_allowed("x.claude.ai", 443, allow))
    check("only 443", not host_allowed("api.anthropic.com", 80, allow))
    check("a path in the host refused", not host_allowed("evil.test/.anthropic.com", 443, allow))
    check("an @ in the host refused", not host_allowed("evil.test@x.anthropic.com", 443, allow))

    settings = SandboxSettings(project="/var/tmp/p", home="/var/tmp/h")
    profile = seatbelt_profile(settings, 1234, "/var/tmp/s")
    check("profile denies network first", profile.index("(deny network*)") < profile.index('"localhost:1234"'))
    check("profile keeps other loopback closed", 'localhost:*"))\n(allow network-bind' not in profile)
    check("profile hides secrets after allowing writes", profile.index("(deny file-read*") > profile.index("(deny file-write*)"))
    # The CLI's own login lives here: hide it and a subscription login looks logged out.
    check("keychain isn't hidden", 'deny file-read* (subpath "/private/var/tmp/h/Library/Keychains"' not in profile)
    check("keychain can rotate a token", "Library/Keychains" in profile and "(allow file-write*" in profile)
    check("profile keeps audio out", "audio" in profile and "/usr/bin/afplay" in profile)
    loose = seatbelt_profile(SandboxSettings(project="/var/tmp/p", home="/var/tmp/h", allow_localhost=True), 1, "/var/tmp/s")
    check("--sandbox-localhost opens loopback", '(allow network-outbound (remote ip "localhost:*"))' in loose)
    if sys.platform != "win32":
        tricky = seatbelt_profile(SandboxSettings(project='/var/tmp/a"b(c', home="/var/tmp/h"), 1, "/var/tmp/s")
        check("odd paths stay quoted", '/a\\"b(c"' in tricky)

    argv = bwrap_argv(settings, "/var/tmp/s", 18443)
    check("bwrap shares nothing", "--unshare-all" in argv and "--share-net" not in argv)
    check("bwrap root is read-only", argv[1:4] == ["--ro-bind", "/", "/"])


def test_refuses_without_a_sandbox() -> None:
    real = sandbox.available
    sandbox.available = lambda: "no sandbox here"
    try:
        box = Sandbox(SandboxSettings(project="."))
        try:
            asyncio.run(box.start())
            check("refuses to start unsandboxed", False)
        except sandbox.SandboxUnavailable:
            check("refuses to start unsandboxed", True)
    finally:
        sandbox.available = real


def main() -> int:
    test_pure()
    test_refuses_without_a_sandbox()
    asyncio.run(test_proxy_rules())
    reason = sandbox.available()
    if reason:
        print(f"(skipping the live sandbox runs: {reason})")
    else:
        asyncio.run(test_control_run())
        asyncio.run(test_hostile_script())
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

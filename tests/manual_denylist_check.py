"""Does the denylist in config.py really stop what it claims? Asks the real CLI.

A local fake Anthropic endpoint answers every session with a tool_use for the
exact command under test, so no model can reword or refuse it and the CLI's own
permission matcher is the only thing deciding. Free, no account, no audio. Run it
after a `claude` upgrade: this file is the proof behind
docs/permission-spike-results.md, and matcher behavior is the CLI's to change.

Every command is harmless and runs inside a throwaway temp folder. Nothing here
needs network, and if a rule stopped matching the worst case is a usage message.

Run:  python -m tests.manual_denylist_check
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from talk2me.config import DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS

STOPPED = [
    "rm -rf d", "rm -fr d", "rm -Rf d", "rm -fR d", "rm -r d", "rm -r -f d", "rm -f -r d",
    "rm -v -rf d", "rm d -rf", "rm --recursive --force d", "rm --force --recursive d",
    "/bin/rm -rf d", "command rm -rf d", "env rm -rf d", "timeout 5 rm -rf d",
    "true && rm -rf d", "echo hi; rm -rf d", "echo d | xargs rm -rf", "sh -c 'rm -rf d'",
    "find . -name marker -delete", "find . -name marker -exec rm {} ;",
    "python3 -c 'print(1)'", "python3.12 -c 'print(1)'", "python -c 'print(1)'",
    "./venv/bin/python -c 'print(1)'", "echo 'print(1)' | python3",
    "node -e 'console.log(1)'", "perl -e 'print 1'", "ruby -e 'puts 1'",
    "bash -c 'echo 1'", "sh -c 'echo 1'", "zsh -c 'echo 1'", "/bin/sh -c 'echo 1'",
    "cat marker | sh", "eval 'echo 1'", "osascript -e 'return 1'",
    "curl --version", "/usr/bin/curl --version", "/usr/bin/ssh -V", "scp x y", "/usr/bin/scp x y",
    "rsync --version", "nc -h", "/usr/bin/nc -h", "telnet", "git push", "git -C . push",
    "git --no-pager push", "git -C . reset --hard", "git clean -n", "git branch -D nope",
    "sudo -n true", "/usr/bin/sudo -n true", "su -h", "security list-keychains",
]
# Everyday commands the rules must NOT catch.
ALLOWED = [
    "rm -f marker", "rm marker", "rm -f report-r.txt", "find . -name marker", "python3 --version",
    "git status", "git commit --allow-empty -m 'push it'", "ncal 2026", "sum marker",
]
# Known gaps, kept here on purpose so nobody reads the list as a sandbox. These
# run on current CLIs. If one starts getting stopped, enjoy it, then update the docs.
KNOWN_GAPS = [
    "rm -vrf d",
    "chmod +x marker && ./marker",
    "printf 'rm -f marker' > s.txt && chmod +x s.txt && ./s.txt",
    "git config alias.p push && git p",
    "npm run nothing",
]


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _tool_use(cmd: str) -> bytes:
    arguments = json.dumps({"command": cmd, "description": "denylist check"})
    return _sse([
        {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "fake", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": arguments}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ])


class _FakeAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        found = re.search(r"CMD<<<(.*?)>>>", json.dumps(body.get("messages", [])))
        out = _tool_use(json.loads(f'"{found.group(1)}"')) if found else b"{}"
        self.send_response(200)
        self.send_header("content-type", "text/event-stream" if found else "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _verdict(cmd: str, env: dict, mode: str, allowed: str) -> str:
    """'stopped' or 'ran', read from the CLI's own decision record."""
    folder = tempfile.mkdtemp(prefix="t2m-denylist-")
    try:
        os.makedirs(os.path.join(folder, "d"))
        open(os.path.join(folder, "marker"), "w").close()
        subprocess.run(["git", "init", "-q", folder], check=True)
        argv = [
            "claude", "-p", f"CMD<<<{cmd}>>>", "--output-format", "stream-json", "--verbose",
            "--model", "haiku", "--permission-mode", mode, "--strict-mcp-config",
            "--setting-sources", "project,local",
            "--disallowedTools", ",".join(DEFAULT_DISALLOWED_TOOLS),
        ]
        if allowed:
            argv += ["--allowedTools", allowed]
        proc = subprocess.Popen(
            argv, cwd=folder, env=env, text=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        timer = threading.Timer(60, proc.kill)
        timer.start()
        verdict = "no answer"
        for line in proc.stdout:
            try:
                meta = json.loads(line).get("tool_result_meta")
            except json.JSONDecodeError:
                continue
            if meta:
                decision = (meta[0].get("permission_decision") or {}).get("decision")
                verdict = "stopped" if decision == "reject" else "ran"
                break
        # The tool result is all we came for; the CLI's next API call goes nowhere.
        proc.kill()
        proc.wait()
        timer.cancel()
        return verdict
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env.update(
        ANTHROPIC_API_KEY="fake",
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
    )
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=False).stdout.strip()
    print(f"claude {version}, {len(DEFAULT_DISALLOWED_TOOLS)} deny rules")

    bypass = ("bypassPermissions", "")
    gated = ("default", ",".join(DEFAULT_ALLOWED_TOOLS))
    cases = (
        [(c, "stopped", *bypass) for c in STOPPED]
        + [(c, "ran", *bypass) for c in ALLOWED]
        + [(c, "ran", *bypass) for c in KNOWN_GAPS]
        # The gated allowlist auto-runs `find:*`: deny has to win over allow.
        + [("find . -name marker -delete", "stopped", *gated)]
        + [("find . -name marker -exec rm {} ;", "stopped", *gated)]
        + [("find . -name marker", "ran", *gated)]
    )
    failed = 0
    with ThreadPoolExecutor(6) as pool:
        verdicts = pool.map(lambda c: _verdict(c[0], env, c[2], c[3]), cases)
        for (cmd, want, mode, _allowed), got in zip(cases, verdicts):
            ok = got == want
            failed += not ok
            tag = "known gap" if cmd in KNOWN_GAPS else want
            print(f"[{'PASS' if ok else 'FAIL'}] {tag:9} {got:9} {mode[:6]:6} {cmd}")
    print(f"[{'PASS' if not failed else 'FAIL'}] overall ({len(cases) - failed}/{len(cases)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

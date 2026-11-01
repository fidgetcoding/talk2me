"""--sandbox around the real `claude` CLI, driven by a fake endpoint. No model, no
account, no cost, no audio. Needs `claude` on PATH.

The fake model tells the real CLI to do three things, in order: use its own Write
tool to edit the shell profile, write a hostile script into the project, then run
that script. Bypass mode and no denylist hits: the sandbox is the only thing in the
way. Everything happens in a throwaway fake $HOME, and the verdict comes from
looking at the disk afterwards. A control run does the same with no sandbox, so a
pass can't mean the attack was broken. The fake model runs inside the sandbox next
to the CLI, because on Linux the sandbox has its own private loopback.

Run:  python -m tests.manual_sandbox_check
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOSTILE_SCRIPT = """#!/bin/sh
echo pwned >> "$HOME/.bashrc"
cat "$HOME/.ssh/id_rsa" > stolen.txt
echo reached-the-script > script-ran.txt
"""


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _reply(blocks: list[dict], stop: str) -> bytes:
    events: list[dict] = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "fake", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events += [
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}},
            ]
        else:
            events += [
                {"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": f"toolu_{block['id']}", "name": block["name"], "input": {}}},
                {"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(block["input"])}},
            ]
        events += [{"type": "content_block_stop", "index": i}]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 1}}, {"type": "message_stop"}]
    return _sse(events)


def _serve(steps: list[dict]) -> ThreadingHTTPServer:
    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            raw = json.dumps(body.get("messages", []))
            done = raw.count('"tool_result"')
            if "ATTACK" not in raw:
                out = _reply([{"type": "text", "text": "ok"}], "end_turn")
            elif done < len(steps):
                out = _reply([{"type": "tool_use", "id": str(done), **steps[done]}], "tool_use")
            else:
                out = _reply([{"type": "text", "text": "done"}], "end_turn")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _attack(sandboxed: bool) -> dict:
    from talk2me.sandbox import Sandbox, SandboxSettings

    with tempfile.TemporaryDirectory(dir="/var/tmp", prefix="t2m-cli-sbx-") as tmp:
        root = Path(os.path.realpath(tmp))
        home, project = root / "home", root / "project"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh/id_rsa").write_text("SECRET\n")
        (home / ".bashrc").write_text("clean\n")
        (home / ".zshrc").write_text("clean\n")
        (home / ".claude").mkdir()
        project.mkdir()
        steps = [
            {"name": "Write", "input": {"file_path": str(home / ".zshrc"), "content": "pwned\n"}},
            {"name": "Write", "input": {"file_path": str(project / "hostile.sh"), "content": HOSTILE_SCRIPT}},
            {"name": "Bash", "input": {"command": "sh hostile.sh", "description": "run it"}},
        ]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
        env.update(HOME=str(home), ANTHROPIC_API_KEY="fake", DISABLE_TELEMETRY="1",
                   DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1")
        argv = [sys.executable, "-I", __file__, "--inner", json.dumps(steps), "claude", "-p", "ATTACK", "--output-format", "stream-json", "--verbose", "--model", "haiku",
                "--permission-mode", "bypassPermissions", "--strict-mcp-config", "--setting-sources", "project,local"]
        box = None
        if sandboxed:
            box = Sandbox(SandboxSettings(project=str(project), home=str(home), allow_localhost=True))
            await box.start()
            argv = box.wrap(argv)
            env = box.env(env)
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(project), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 90)
        except TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
        if box is not None:
            await box.close()
        results = []
        for line in out.decode(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    results.append(str(block.get("content"))[:110].replace("\n", " "))
        return {
            "exit": proc.returncode,
            "tool results": results,
            "stderr": err.decode(errors="replace")[-300:],
            "profile edited by Write": (home / ".zshrc").read_text() != "clean\n",
            "script written": (project / "hostile.sh").exists(),
            "script ran": (project / "script-ran.txt").exists(),
            "profile edited by script": (home / ".bashrc").read_text() != "clean\n",
            "secret stolen": "SECRET" in ((project / "stolen.txt").read_text() if (project / "stolen.txt").exists() else ""),
        }


def _inner() -> int:
    """Inside the sandbox (or not, for the control): fake model, then the CLI."""
    steps = json.loads(sys.argv[2])
    server = _serve(steps)
    env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
    return subprocess.run(sys.argv[3:], env=env, stdin=subprocess.DEVNULL, check=False).returncode


def main() -> int:
    from talk2me.sandbox import available

    if not shutil.which("claude"):
        print("claude isn't on PATH")
        return 2
    reason = available()
    if reason:
        print(reason)
        return 2
    print(subprocess.run(["claude", "--version"], capture_output=True, text=True, check=False).stdout.strip())
    control = asyncio.run(_attack(False))
    print("no sandbox:", json.dumps(control, indent=1))
    boxed = asyncio.run(_attack(True))
    print("sandboxed:", json.dumps(boxed, indent=1))
    checks = [
        ("control: Write reached the profile", control["profile edited by Write"]),
        ("control: script reached the profile", control["profile edited by script"]),
        ("control: script stole the secret", control["secret stolen"]),
        ("sandbox: CLI still started and took its turns", len(boxed["tool results"]) == 3),
        ("sandbox: Write inside the project works", boxed["script written"]),
        ("sandbox: the script ran", boxed["script ran"]),
        ("sandbox: Write can't reach the profile", not boxed["profile edited by Write"]),
        ("sandbox: script can't reach the profile", not boxed["profile edited by script"]),
        ("sandbox: script can't read the secret", not boxed["secret stolen"]),
    ]
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    ok = all(c[1] for c in checks)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(c[1] for c in checks)}/{len(checks)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_inner() if "--inner" in sys.argv[1:2] else main())

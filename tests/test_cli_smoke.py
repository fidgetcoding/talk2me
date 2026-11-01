"""The real `python -m talk2me` as a real process, with a stand-in `claude` on PATH.
Text mode, so no audio on any OS. This is what catches what in-process tests can't:
how the binary gets found, real pipes, and printing to whatever a platform's console
encoding is. It runs on Linux, macOS and Windows in CI.

Run:  python -m tests.test_cli_smoke
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RESULTS: list[bool] = []
FAKE = Path(__file__).with_name("fake_claude_cli.py")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


def _shim(folder: Path) -> None:
    """A `claude` the way an install really leaves one: .cmd on Windows (npm does that)."""
    if sys.platform == "win32":
        (folder / "claude.cmd").write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n')
    else:
        path = folder / "claude"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        path.chmod(0o755)


def _talk2me(args: list[str], typed: str, script: list[dict]) -> tuple[subprocess.CompletedProcess, list[dict]]:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        _shim(folder)
        log = folder / "log.jsonl"
        # Nothing inherited that changes behavior, or writes test turns into a real
        # transcript folder.
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "TALK2ME_SAVE_DIR")}
        env.update(
            PATH=f"{folder}{os.pathsep}{env['PATH']}",
            FAKE_CLAUDE_SCRIPT=json.dumps(script),
            FAKE_CLAUDE_LOG=str(log),
            TALK2ME_CONFIG=str(folder / "no-config.json"),
            TALK2ME_STATE=str(folder / "state.json"),
        )
        proc = subprocess.run(
            [sys.executable, "-m", "talk2me", "--cwd", str(folder), *args],
            input=typed.encode(), capture_output=True, env=env, timeout=60, check=False,
        )
        events = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return proc, events


def test_text_turn() -> None:
    # The emoji is the point: on Windows a pipe defaults to a code page that has none.
    proc, events = _talk2me(["--text"], "hello there\n", [{"say": "Hi back ✅ 🎉"}])
    out = proc.stdout.decode("utf-8", errors="replace")
    check("text turn exits cleanly", proc.returncode == 0, proc.stderr.decode(errors="replace")[-300:])
    check("the agent got what was typed", {"event": "user", "text": "hello there"} in events, str(events))
    check("the reply printed", "Hi back" in out, out[-200:])


def test_gate_typed() -> None:
    proc, events = _talk2me(["--text", "--gated"], "run the tests\ny\nnow push it\nn\n", [
        {"ask": "npm test", "say": "Tests pass."},
        {"ask": "git push", "say": "Okay, not pushing."},
    ])
    verdicts = [e["behavior"] for e in events if e.get("event") == "permission"]
    check("gate approves and denies through real pipes", verdicts == ["allow", "deny"], str(verdicts))
    check("gate run exits cleanly", proc.returncode == 0, proc.stderr.decode(errors="replace")[-300:])


def test_denylist_reaches_the_cli() -> None:
    _, events = _talk2me(["--text"], "hi\n", [{"say": "ok"}])
    argv = next(e["argv"] for e in events if e.get("event") == "started")
    denied = argv[argv.index("--disallowedTools") + 1]
    check("denylist is on the real command line", "Bash(rm -rf:*)" in denied and "Bash(*/curl *)" in denied)
    check("text mode keeps its default posture", argv[argv.index("--permission-mode") + 1] == "bypassPermissions")


def test_malformed_agent_output() -> None:
    """Junk on the wire costs that line. Everything here goes through the real reader."""
    esc = "\x1b"
    junk = [
        "this is not json", "[1, 2, 3]", "5", "null", '"a string"', "{}",
        json.dumps({"type": "stream_event", "event": "not a dict"}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": "nope"}}),
        json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": 5}}}),
        json.dumps({"type": "assistant", "message": "not a dict"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": None, "input": "str"}]}}),
        json.dumps({"type": "control_request", "request": "not a dict"}),
        '{"type": "stream_event", "event": {"type": "content_block_delta"',  # torn
    ]
    proc, _ = _talk2me(["--text"], "go\n", [{"raw": junk, "huge": 20 * 1024 * 1024, "say": f"Clean {esc}]0;pwned{esc}\\{esc}[31mreply"}])
    out = proc.stdout.decode("utf-8", errors="replace")
    check("junk from the agent doesn't end the session", proc.returncode == 0, proc.stderr.decode(errors="replace")[-200:])
    check("the real reply gets through", "Clean" in out and "reply" in out, out[-160:])
    check("escape codes never reach the terminal", esc not in out)


def test_agent_dies() -> None:
    proc, _ = _talk2me(["--text"], "go\n", [{"die": 3}])
    err = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
    check("agent dying mid-turn: says so, exits 1", proc.returncode == 1 and "rc=3" in err and "Traceback" not in err, err[-200:])
    proc, _ = _talk2me(["--text", "--gated"], "go\ny\n", [{"ask": "npm test", "die": 3}])
    err = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
    check("agent dying during a verdict: no traceback", proc.returncode == 1 and "Traceback" not in err, err[-200:])


def test_harmless_flags() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["TALK2ME_CONFIG"] = os.devnull
    proc = subprocess.run([sys.executable, "-m", "talk2me", "--help"], capture_output=True, env=env, timeout=60, check=False)
    check("--help runs", proc.returncode == 0, proc.stderr.decode(errors="replace")[-200:])


def main() -> int:
    test_text_turn()
    test_gate_typed()
    test_denylist_reaches_the_cli()
    test_malformed_agent_output()
    test_agent_dies()
    test_harmless_flags()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

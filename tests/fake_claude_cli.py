"""Stands in for the `claude` binary in end-to-end tests. Speaks the same stream-json
wire the real CLI does (pinned in docs/permission-spike-results.md), with no model
and no account. Each turn comes from FAKE_CLAUDE_SCRIPT, and what it sees goes to
FAKE_CLAUDE_LOG as JSON lines, so the test can check what really came through.

A script is a JSON list with one entry per user turn:
  {"ask": "npm test", "say": "Done."}   ask permission for Bash, then reply
  {"say": "..."}                        just reply
  {"say": "...", "hold": 20}            reply, then keep the turn open up to 20 s
                                        unless an interrupt arrives
"""

import json
import os
import select
import sys
import time

SCRIPT = json.loads(os.environ.get("FAKE_CLAUDE_SCRIPT", "[]"))
LOG = os.environ.get("FAKE_CLAUDE_LOG")


def log(**event) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def say(text: str) -> None:
    emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}})


def finish(text: str, subtype: str = "success") -> None:
    emit({"type": "result", "subtype": subtype, "result": text, "session_id": "fake", "is_error": subtype != "success"})


def read_message(timeout: float | None = None) -> dict | None:
    if timeout is not None and sys.platform != "win32":
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
    line = sys.stdin.readline()
    if not line:
        log(event="stdin closed")
        raise SystemExit(0)
    return json.loads(line)


def run_turn(turn: dict) -> None:
    if "ask" in turn:
        emit({
            "type": "control_request",
            "request_id": "req-1",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": turn["ask"]}},
        })
        reply = read_message()
        behavior = reply["response"]["response"]["behavior"]
        log(event="permission", behavior=behavior)
    say(turn.get("say", "ok."))
    deadline = time.monotonic() + turn.get("hold", 0)
    while time.monotonic() < deadline:
        msg = read_message(deadline - time.monotonic())
        if msg is None:
            break
        if (msg.get("request") or {}).get("subtype") == "interrupt":
            log(event="interrupt")
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"]}})
            finish(turn.get("say", ""), "error_during_execution")
            return
    finish(turn.get("say", ""))


def main() -> int:
    log(event="started", argv=sys.argv[1:])
    emit({"type": "system", "subtype": "init", "session_id": "fake"})
    turns = iter(SCRIPT)
    while True:
        msg = read_message()
        if msg.get("type") != "user":
            continue
        text = msg["message"]["content"][0]["text"]
        log(event="user", text=text)
        run_turn(next(turns, {"say": "ok."}))


if __name__ == "__main__":
    raise SystemExit(main())

"""Pure-translation test for ClaudeCodeBackend. No subprocess, no audio, no model.

Feeds synthetic stream-json dicts straight into the backend's pure `_translate`
methods and asserts they map onto the closed `events` set. We construct the
backend WITHOUT calling start() — the translate methods only touch self._turn_text,
never the process.

Run:  ./.venv/bin/python -m tests.test_backend_translate
"""

from talk2me.backends.claude_code import ClaudeCodeBackend
from talk2me.events import (
    AssistantTextDelta,
    SessionReady,
    ToolActivity,
    TurnComplete,
)


def _fresh() -> ClaudeCodeBackend:
    """A backend with a clean _turn_text and no process behind it."""
    return ClaudeCodeBackend()


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    return ok


def test_session_ready() -> bool:
    b = _fresh()
    got = b._translate({"type": "system", "subtype": "init", "session_id": "abc"})
    ok = got == [SessionReady(session_id="abc")]
    return _report("system/init -> SessionReady(session_id='abc')", ok)


def test_text_delta() -> bool:
    b = _fresh()
    obj = {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hi"},
        },
    }
    got = b._translate(obj)
    # Maps to a single AssistantTextDelta AND appends the text to _turn_text.
    mapped = got == [AssistantTextDelta(text="Hi")]
    accumulated = b._turn_text == ["Hi"]
    return _report("content_block_delta -> AssistantTextDelta + _turn_text append",
                   mapped and accumulated)


def test_tool_use_via_stream_event() -> bool:
    b = _fresh()
    obj = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Bash"},
        },
    }
    got = b._translate(obj)
    # ToolActivity carries name; summary defaults to "" so compare on name only.
    ok = (
        len(got) == 1
        and isinstance(got[0], ToolActivity)
        and got[0].name == "Bash"
    )
    return _report("content_block_start tool_use -> ToolActivity(name='Bash')", ok)


def test_tool_use_via_assistant_message() -> bool:
    b = _fresh()
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ignored on this path"},
                {"type": "tool_use", "name": "Read"},
            ],
        },
    }
    got = b._translate(obj)
    # Assistant-message path emits ONLY tool activity (text already streamed via
    # deltas), so the text block must not produce an event.
    ok = (
        len(got) == 1
        and isinstance(got[0], ToolActivity)
        and got[0].name == "Read"
    )
    return _report("assistant message tool_use block -> [ToolActivity], text dropped", ok)


def test_result_rollup_and_clear() -> bool:
    b = _fresh()
    # Two deltas accumulate, then a result rolls them up and clears the buffer.
    b._translate({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hi"},
        },
    })
    b._translate({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "There"},
        },
    })
    pre = b._turn_text == ["Hi", "There"]
    got = b._translate({"type": "result"})
    rolled = got == [TurnComplete(text="HiThere")]
    cleared = b._turn_text == []
    return _report("result -> TurnComplete(text='HiThere') + _turn_text cleared",
                   pre and rolled and cleared)


def test_unknown_type() -> bool:
    b = _fresh()
    got = b._translate({"type": "whatever"})
    # Unknown shapes degrade gracefully to no events and touch nothing.
    ok = got == [] and b._turn_text == []
    return _report("unknown type -> []", ok)


def main() -> int:
    results = [
        test_session_ready(),
        test_text_delta(),
        test_tool_use_via_stream_event(),
        test_tool_use_via_assistant_message(),
        test_result_rollup_and_clear(),
        test_unknown_type(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall "
          f"({sum(results)}/{len(results)} groups passed)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

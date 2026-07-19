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
    # Simulate a partial having already streamed this turn, so the assistant
    # message's text must be suppressed (no double-speak) and only the tool
    # block surfaces.
    b._translate({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "already streamed"},
        },
    })
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
    # Text already streamed via deltas, so the text block must NOT re-emit;
    # only the tool block produces an event.
    ok = (
        len(got) == 1
        and isinstance(got[0], ToolActivity)
        and got[0].name == "Read"
    )
    return _report("assistant message (after partials) -> [ToolActivity], text suppressed", ok)


def test_assistant_message_text_fallback() -> bool:
    b = _fresh()
    # No partials streamed this turn — a full assistant message carrying text
    # must produce an AssistantTextDelta so the turn isn't silent dead-air
    # (review I4). Text is also accumulated for the TurnComplete rollup.
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
        },
    }
    got = b._translate(obj)
    emitted = got == [AssistantTextDelta(text="Hello world")]
    accumulated = b._turn_text == ["Hello world"]
    streamed_flag = b._turn_streamed is True
    return _report(
        "assistant message, no prior partials -> AssistantTextDelta fallback",
        emitted and accumulated and streamed_flag,
    )


def test_assistant_message_no_double_speak() -> bool:
    b = _fresh()
    # A delta streams first, THEN the full assistant message arrives with the
    # same text. The fallback must NOT fire (no double-speak).
    b._translate({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Streamed text"},
        },
    })
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Streamed text"}],
        },
    }
    got = b._translate(obj)
    no_event = got == []
    # _turn_text still holds only the streamed delta, not a duplicate.
    no_dupe = b._turn_text == ["Streamed text"]
    return _report(
        "assistant message after partials -> no fallback, no double-speak",
        no_event and no_dupe,
    )


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


def test_tool_summary_extraction() -> bool:
    from talk2me.backends.claude_code import _summarize_tool_input

    checks = [
        # File tools -> basename only, never the full path.
        _summarize_tool_input("Write", {"file_path": "/tmp/x/pong.html"})
        == "pong.html",
        _summarize_tool_input("Edit", {"file_path": "a/b/app.py"}) == "app.py",
        _summarize_tool_input("Read", {"file_path": "notes.md"}) == "notes.md",
        # Bash -> whitespace-normalized head of the command, capped at 60.
        _summarize_tool_input("Bash", {"command": "ls   -la\n/tmp"})
        == "ls -la /tmp",
        len(_summarize_tool_input("Bash", {"command": "x" * 200})) == 60,
        _summarize_tool_input("Bash", {"command": "x" * 200}).endswith("…"),
        # Grep/Glob -> the pattern; WebFetch -> the url.
        _summarize_tool_input("Grep", {"pattern": "def main"}) == "def main",
        _summarize_tool_input("Glob", {"pattern": "**/*.py"}) == "**/*.py",
        _summarize_tool_input("WebFetch", {"url": "https://x.dev"})
        == "https://x.dev",
        # Unknown tools and empty input stay silent.
        _summarize_tool_input("TodoWrite", {"todos": []}) == "",
        _summarize_tool_input("Bash", {}) == "",
        # Control characters (ANSI escapes) are stripped, not printed.
        "\x1b" not in _summarize_tool_input(
            "Write", {"file_path": "/tmp/\x1b[31mevil\x1b[0m.txt"}
        ),
    ]
    return _report(
        f"tool summary extraction ({sum(checks)}/{len(checks)} checks)",
        all(checks),
    )


def test_tool_upgrade_tagging() -> bool:
    b = _fresh()
    # Stream path: EARLY announcement — name only, upgrade=False.
    early = b._translate({
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Write"},
        },
    })
    # Full-message path: the same call with arguments — upgrade=True + summary.
    late = b._translate({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "/tmp/pong.html", "content": "hi"},
                },
            ],
        },
    })
    ok = (
        early == [ToolActivity(name="Write")]
        and early[0].upgrade is False
        and late == [ToolActivity(name="Write", summary="pong.html", upgrade=True)]
    )
    return _report("stream start = early (no upgrade); full message = upgrade + summary", ok)


def test_sessionlog_tool_detail() -> bool:
    import tempfile

    from talk2me.sessionlog import SessionLog

    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, model=None, stt="whisper", cwd=None)
        log.tool("Write", "pong.html")
        log.tool("Bash")
        with open(log.path, encoding="utf-8") as fh:
            content = fh.read()
    ok = "- 🔧 Write (pong.html)\n" in content and "- 🔧 Bash\n" in content
    return _report("SessionLog.tool detail -> '- 🔧 Write (pong.html)'", ok)


def main() -> int:
    results = [
        test_session_ready(),
        test_text_delta(),
        test_tool_use_via_stream_event(),
        test_tool_use_via_assistant_message(),
        test_assistant_message_text_fallback(),
        test_assistant_message_no_double_speak(),
        test_result_rollup_and_clear(),
        test_unknown_type(),
        test_tool_summary_extraction(),
        test_tool_upgrade_tagging(),
        test_sessionlog_tool_detail(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall "
          f"({sum(results)}/{len(results)} groups passed)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

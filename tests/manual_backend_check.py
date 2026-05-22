"""Manual smoke test for ClaudeCodeBackend — no audio, no cost beyond one turn.

Spawns Claude Code in stream-json, sends one trivial turn, prints normalized
events, asserts we saw SessionReady -> text -> TurnComplete. This is the spine
check: if this passes, the injection + filtering problems are solved.

Run:  ./.venv/bin/python -m tests.manual_backend_check
"""

import asyncio

from talk2me.backends import ClaudeCodeBackend
from talk2me.events import (
    AssistantTextDelta,
    BackendError,
    SessionReady,
    ToolActivity,
    TurnComplete,
)


async def main() -> int:
    backend = ClaudeCodeBackend(model="haiku", permission_mode="default")
    await backend.start()
    # Claude won't emit `init` until it has input — send the first turn now.
    await backend.send("Reply with exactly: hello from talk2me. No tools.")

    saw_ready = False
    saw_text = False
    saw_complete = False
    spoken: list[str] = []

    async def consume() -> None:
        nonlocal saw_ready, saw_text, saw_complete
        async for ev in backend.events():
            if isinstance(ev, SessionReady):
                saw_ready = True
                print(f"[ready] session={ev.session_id}")
            elif isinstance(ev, AssistantTextDelta):
                saw_text = True
                spoken.append(ev.text)
                print(ev.text, end="", flush=True)
            elif isinstance(ev, ToolActivity):
                print(f"\n[tool] {ev.name}")
            elif isinstance(ev, TurnComplete):
                saw_complete = True
                print(f"\n[turn-complete] rollup={ev.text!r}")
                return
            elif isinstance(ev, BackendError):
                print(f"\n[error] {ev.message}")
                return

    try:
        await asyncio.wait_for(consume(), timeout=90)
    except asyncio.TimeoutError:
        print("\n[timeout] no TurnComplete in 90s")
    finally:
        await backend.close()

    ok = saw_ready and saw_text and saw_complete
    print(f"\n\nRESULT: ready={saw_ready} text={saw_text} complete={saw_complete} "
          f"-> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

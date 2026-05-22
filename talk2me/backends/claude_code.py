"""Claude Code backend — the spine of talk2me.

Runs `claude` headless in bidirectional stream-json:

    claude -p --input-format stream-json --output-format stream-json \
           --include-partial-messages --replay-user-messages [--session-id UUID]

One long-lived process == one conversation. We write user turns as JSON lines to
stdin and parse a stream of JSON events from stdout, normalizing them into the
closed `events` set. This is what kills the two hard problems of a terminal voice
broker: text injection (we own stdin) and output filtering (we read structured
events, not a scraped TUI — only assistant *text* is spoken; tool noise is shown
but never voiced).

Stream-json schema is treated defensively: unknown event shapes are ignored, not
fatal, so a Claude Code version bump degrades gracefully instead of crashing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from ..events import (
    AgentEvent,
    AssistantTextDelta,
    BackendError,
    SessionReady,
    ToolActivity,
    TurnComplete,
)


class ClaudeCodeBackend:
    """AgentBackend over the Claude Code CLI's stream-json transport."""

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        model: str | None = None,
        cwd: str | None = None,
        permission_mode: str = "default",
        session_id: str | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self._bin = claude_bin
        self._model = model
        self._cwd = cwd
        self._permission_mode = permission_mode
        self._session_id = session_id or str(uuid.uuid4())
        self._extra_args = extra_args or []

        self._proc: asyncio.subprocess.Process | None = None
        self._events: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # Accumulates partial text deltas for the current assistant message so we
        # can emit a clean rollup on TurnComplete.
        self._turn_text: list[str] = []

    # ---- lifecycle -------------------------------------------------------

    def _argv(self) -> list[str]:
        argv = [
            self._bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--replay-user-messages",
            "--verbose",  # required for stream-json to emit the full event stream
            "--session-id",
            self._session_id,
            "--permission-mode",
            self._permission_mode,
        ]
        if self._model:
            argv += ["--model", self._model]
        argv += self._extra_args
        return argv

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def send(self, user_text: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("backend not started")
        self._turn_text.clear()
        msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            },
        }
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    def events(self) -> AsyncIterator[AgentEvent]:
        return self._event_iter()

    async def _event_iter(self) -> AsyncIterator[AgentEvent]:
        while True:
            ev = await self._events.get()
            yield ev

    async def interrupt(self) -> None:
        # Claude Code's stream-json input has no documented mid-turn interrupt.
        # Barge-in is handled by the orchestrator stopping TTS playback locally;
        # this is a structured no-op so callers don't special-case backends.
        return None

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass

    # ---- stdout parsing --------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break  # EOF — process exited
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise; ignore
                for ev in self._translate(obj):
                    await self._events.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive: never let the reader die silently
            await self._events.put(BackendError(f"stdout reader: {exc!r}"))
        finally:
            rc = self._proc.returncode if self._proc else None
            if rc not in (None, 0):
                await self._events.put(BackendError(f"claude exited rc={rc}"))

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                # stderr is diagnostic; surface only if it looks like a hard error.
                txt = raw.decode("utf-8", errors="replace").strip()
                if txt and ("error" in txt.lower() or "fatal" in txt.lower()):
                    await self._events.put(BackendError(f"stderr: {txt}"))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _translate(self, obj: dict) -> list[AgentEvent]:
        """Map one stream-json object to zero or more normalized events."""
        t = obj.get("type")

        if t == "system" and obj.get("subtype") == "init":
            return [SessionReady(session_id=obj.get("session_id"))]

        # Partial token deltas (from --include-partial-messages): the raw
        # Anthropic streaming events, wrapped under "stream_event"/"event".
        if t == "stream_event":
            return self._translate_stream_event(obj.get("event") or {})

        # Full assistant message (also arrives without partials enabled).
        if t == "assistant":
            return self._translate_assistant_message(obj.get("message") or {})

        # Turn boundary.
        if t == "result":
            rollup = "".join(self._turn_text).strip()
            self._turn_text.clear()
            return [TurnComplete(text=rollup)]

        return []

    def _translate_stream_event(self, event: dict) -> list[AgentEvent]:
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    self._turn_text.append(text)
                    return [AssistantTextDelta(text=text)]
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                return [ToolActivity(name=block.get("name", "tool"))]
        return []

    def _translate_assistant_message(self, message: dict) -> list[AgentEvent]:
        # Fallback path when partials are absent: extract whole text/tool blocks.
        # When partials ARE enabled we'd double-speak text, so only emit tool
        # activity here (text already streamed via stream_event deltas).
        out: list[AgentEvent] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                out.append(ToolActivity(name=block.get("name", "tool")))
        return out

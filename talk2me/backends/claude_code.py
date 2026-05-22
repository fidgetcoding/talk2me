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
import re
import uuid
from collections.abc import AsyncIterator

# Generous per-line ceiling for the stdout/stderr StreamReaders. The default is
# 64 KiB, past which readline() raises LimitOverrunError and wedges the stream
# (security H2). A single JSON event — even one carrying a large tool result —
# stays well under 16 MiB, while the cap still bounds memory against a runaway
# or hostile line.
_STREAM_LIMIT = 16 * 1024 * 1024

# Matches C0/C1 control characters except tab/newline/carriage-return, so we can
# strip ANSI escapes and other terminal-control injection out of stderr before
# it is ever surfaced (security L1).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _sanitize(text: str) -> str:
    """Strip terminal control characters so child stderr can't inject escapes."""
    return _CONTROL_CHARS.sub("", text)

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
        # Whether any text delta streamed for the current turn. Drives the
        # full-message fallback: if a complete `assistant` message arrives with
        # text but no partials streamed, we emit that text so the turn isn't
        # silent dead-air. When partials DID stream, we suppress it to avoid
        # double-speak (review I4).
        self._turn_streamed = False

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
            limit=_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def send(self, user_text: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("backend not started")
        self._turn_text.clear()
        self._turn_streamed = False
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
        # Cancel the reader/stderr tasks, then AWAIT them so they fully unwind
        # before we touch the process — otherwise asyncio emits "Task destroyed
        # but it is pending" warnings and a half-cancelled reader can still put
        # onto the queue after close (review I7).
        tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

        if self._proc is None:
            return

        # Guarantee the child is reaped: if terminate() doesn't land within the
        # grace window, escalate to kill() and STILL wait() so no zombie is left
        # behind (review I8).
        if self._proc.returncode is None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                await self._proc.wait()

    # ---- stdout parsing --------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        stdout = self._proc.stdout
        try:
            while True:
                try:
                    raw = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # An over-long line blew past the StreamReader limit. Rather
                    # than wedge the stream, skip the offending bytes up to the
                    # next newline and resync on the following line (security H2).
                    await self._resync(stdout)
                    continue
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
            # Non-zero returncode is the authoritative "backend died" signal
            # (review I6); stderr is advisory diagnostics only.
            rc = self._proc.returncode if self._proc else None
            if rc not in (None, 0):
                await self._events.put(BackendError(f"claude exited rc={rc}"))

    @staticmethod
    async def _resync(reader: asyncio.StreamReader) -> None:
        """Discard buffered bytes through the next newline after a limit overrun.

        readline()/readuntil() leave the over-long data in the buffer and keep
        raising until it is consumed. We read fixed chunks until a newline shows
        up (or EOF), throwing the partial line away so the next readline() starts
        clean. Errors here are swallowed — resync is best-effort by definition.
        """
        while True:
            try:
                # read(n) returns up to n buffered/available bytes and is NOT
                # bounded by the line limit, so it can never re-trigger the
                # overrun while we drain the offending line.
                chunk = await reader.read(64 * 1024)
            except Exception:
                return
            if not chunk:
                return  # EOF
            nl = chunk.rfind(b"\n")
            if nl != -1:
                # Push back everything after the newline so the next readline()
                # sees the start of a fresh line.
                reader.feed_data(chunk[nl + 1:])
                return

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        stderr = self._proc.stderr
        try:
            while True:
                try:
                    raw = await stderr.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # Bound the stderr reader the same way as stdout (security
                    # H2): skip an over-long diagnostic line and keep draining.
                    await self._resync(stderr)
                    continue
                if not raw:
                    break
                # stderr is advisory diagnostics ONLY. We do NOT derive failure
                # from a substring heuristic — "0 errors" false-positives and a
                # silent crash false-negatives (review I6). The authoritative
                # death signal is a non-zero process returncode, handled in
                # _read_stdout's finally. We strip control characters so a child
                # can't inject ANSI/terminal escapes (security L1), then drain.
                _sanitize(raw.decode("utf-8", errors="replace").strip())
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
            self._turn_streamed = False
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
                    self._turn_streamed = True
                    return [AssistantTextDelta(text=text)]
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                return [ToolActivity(name=block.get("name", "tool"))]
        return []

    def _translate_assistant_message(self, message: dict) -> list[AgentEvent]:
        # A full assistant message. Tool blocks always surface here.
        #
        # Text is the subtle case. When --include-partial-messages is on, prose
        # already streamed as text_delta events, so re-emitting the whole text
        # block would double-speak — we suppress it. But if NO delta streamed
        # for this turn (partials disabled, or a Claude Code version that stops
        # emitting them), the turn would be silent dead-air. So we make the
        # fallback real (review I4): emit the message's text as a single
        # AssistantTextDelta, accumulate it for the TurnComplete rollup, and mark
        # the turn as streamed so a follow-up assistant message doesn't re-speak.
        out: list[AgentEvent] = []
        text_parts: list[str] = []
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                out.append(ToolActivity(name=block.get("name", "tool")))
            elif btype == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)

        if text_parts and not self._turn_streamed:
            full = "".join(text_parts)
            self._turn_text.append(full)
            self._turn_streamed = True
            out.append(AssistantTextDelta(text=full))
        return out

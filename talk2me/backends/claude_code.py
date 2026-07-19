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
import os
import re
import uuid
from collections.abc import AsyncIterator

from ..events import (
    AgentEvent,
    AssistantTextDelta,
    BackendError,
    PermissionRequest,
    SessionReady,
    ThinkingDelta,
    ToolActivity,
    TurnComplete,
)

# Generous per-line ceiling for the stdout/stderr StreamReaders. The default is
# 64 KiB, past which readuntil() raises LimitOverrunError and wedges the stream
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


# Human summary of a tool call's input, for the transcript line. Deliberately
# tiny: a filename, a pattern, the head of a command — never the full payload.
_SUMMARY_MAX = 60


def _summarize_tool_input(name: str, inp: dict) -> str:
    """One short, sanitized fragment describing what the tool is touching."""
    if not isinstance(inp, dict) or not inp:
        return ""
    if name in ("Write", "Edit", "Read"):
        path = str(inp.get("file_path", ""))
        summary = os.path.basename(path.rstrip("/")) if path else ""
    elif name == "Bash":
        summary = " ".join(str(inp.get("command", "")).split())
    elif name in ("Grep", "Glob"):
        summary = str(inp.get("pattern", ""))
    elif name == "WebFetch":
        summary = str(inp.get("url", ""))
    else:
        return ""
    if len(summary) > _SUMMARY_MAX:
        summary = summary[: _SUMMARY_MAX - 1] + "…"
    return _sanitize(summary)


# Ceilings for the multi-line tool body (the actual code / diff / command
# shown in the feed). Enough to SEE the work; never the whole payload.
_BODY_MAX_LINES = 20
_BODY_MAX_CHARS = 2000
_EDIT_SIDE_LINES = 8


def _clip_body(text: str) -> str:
    """Bound a body preview by chars then lines, with an honest '+N more'."""
    text = text[:_BODY_MAX_CHARS]
    lines = text.splitlines()
    if len(lines) > _BODY_MAX_LINES:
        dropped = len(lines) - _BODY_MAX_LINES
        lines = lines[:_BODY_MAX_LINES] + [f"… (+{dropped} more lines)"]
    return _sanitize("\n".join(lines))


def _tool_body(name: str, inp: dict) -> str:
    """The work itself: file content for Write, a -/+ diff for Edit, the full
    command for Bash. Empty for tools whose one-line summary says it all."""
    if not isinstance(inp, dict) or not inp:
        return ""
    if name == "Write":
        return _clip_body(str(inp.get("content", "")))
    if name == "Edit":
        old = str(inp.get("old_string", ""))
        new = str(inp.get("new_string", ""))
        if not (old or new):
            return ""

        def side(prefix: str, s: str) -> list[str]:
            lines = s.splitlines() or [""]
            out = [f"{prefix} {line}" for line in lines[:_EDIT_SIDE_LINES]]
            if len(lines) > _EDIT_SIDE_LINES:
                out.append(f"{prefix} … (+{len(lines) - _EDIT_SIDE_LINES} more lines)")
            return out

        return _clip_body("\n".join(side("-", old) + side("+", new)))
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        # The one-line summary already shows short commands — a body would
        # just duplicate it. Long or multi-line commands earn the full view.
        if len(cmd) <= _SUMMARY_MAX and "\n" not in cmd:
            return ""
        return _clip_body(cmd)
    return ""


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
        permission_prompt_stdio: bool = False,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        setting_sources: str | None = None,
        append_system_prompt: str | None = None,
        extra_env: dict[str, str] | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        self._bin = claude_bin
        self._model = model
        self._cwd = cwd
        self._permission_mode = permission_mode
        # Resuming keeps the SAME session id (spike-verified) — record it as
        # ours so continuity state stays consistent across resumed launches.
        self._resume_session_id = resume_session_id
        self._session_id = resume_session_id or session_id or str(uuid.uuid4())
        self._extra_args = extra_args or []
        # Wire `--permission-prompt-tool stdio`: an unresolved tool call pauses
        # the turn and surfaces here as a control_request instead of a silent
        # deny. The host answers via respond_permission().
        self._permission_prompt_stdio = permission_prompt_stdio
        self._allowed_tools = allowed_tools or []
        self._disallowed_tools = disallowed_tools or []
        # e.g. "project,local" to drop the user-level config (hooks, skills,
        # user CLAUDE.md) — a measured time-to-first-token win for voice.
        self._setting_sources = setting_sources
        self._append_system_prompt = append_system_prompt
        # Extra environment for the child — how OTHER brains ride this same
        # backend: Kimi/GLM/DeepSeek publish Anthropic-compatible endpoints,
        # so ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN aim the `claude` CLI
        # at them while every event-protocol behavior stays identical.
        self._extra_env = dict(extra_env or {})

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
        ]
        if self._resume_session_id:
            argv += ["--resume", self._resume_session_id]
        else:
            argv += ["--session-id", self._session_id]
        argv += [
            "--permission-mode",
            self._permission_mode,
        ]
        if self._model:
            argv += ["--model", self._model]
        if self._permission_prompt_stdio:
            argv += ["--permission-prompt-tool", "stdio"]
        # Rules never contain commas, so the comma-joined single-token form is
        # unambiguous (the variadic space-separated form would swallow a
        # following positional).
        if self._allowed_tools:
            argv += ["--allowedTools", ",".join(self._allowed_tools)]
        if self._disallowed_tools:
            argv += ["--disallowedTools", ",".join(self._disallowed_tools)]
        if self._setting_sources:
            argv += ["--setting-sources", self._setting_sources]
        if self._append_system_prompt:
            argv += ["--append-system-prompt", self._append_system_prompt]
        argv += self._extra_args
        return argv

    async def start(self) -> None:
        env = None
        if self._extra_env:
            env = {**os.environ, **self._extra_env}
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=env,
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
        """Cancel the in-flight turn via the stdio control protocol.

        Verified on CLI 2.1.214 (docs/permission-spike-results.md): the CLI acks
        with a control_response, generation stops, and the turn ends with
        `result subtype=error_during_execution` — which _translate maps to a
        normal TurnComplete. The session stays alive for the next turn.
        Best-effort: any write failure means the process is already dying, and
        the reader task will surface that as a BackendError.
        """
        if self._proc is None or self._proc.stdin is None:
            return
        msg = {
            "type": "control_request",
            "request_id": f"int_{uuid.uuid4().hex[:8]}",
            "request": {"subtype": "interrupt"},
        }
        try:
            self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionError, RuntimeError, OSError):
            return

    async def respond_permission(
        self, request_id: str, allow: bool, *, message: str | None = None
    ) -> None:
        """Answer a PermissionRequest by writing a control_response to stdin.

        The CLI blocks the turn until the matching request_id arrives (no
        timeout observed — a voice round-trip is safe). Shape pinned in
        docs/permission-spike-results.md.
        """
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("backend not started")
        inner: dict = {"behavior": "allow" if allow else "deny"}
        if not allow:
            inner["message"] = message or "Denied by voice"
        msg = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": inner,
            },
        }
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def switch_session(self, resume_session_id: str) -> None:
        """Swap the live conversation for an earlier one (the spoken session
        picker): stop the current CLI, drop its queued events, relaunch
        resuming the chosen session. The event ITERATOR survives — it reads
        the same queue the new process feeds."""
        await self.close()
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._resume_session_id = resume_session_id
        self._session_id = resume_session_id
        self._turn_text.clear()
        self._turn_streamed = False
        await self.start()

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
                    raw = await stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as e:
                    # EOF. e.partial may carry a final unterminated line; process
                    # it, then the next iteration raises again with b"" -> break.
                    raw = e.partial
                    if not raw:
                        break  # EOF — process exited
                except asyncio.LimitOverrunError as e:
                    # An over-long line blew past the StreamReader limit. Discard
                    # exactly the offending bytes and resync (security H2).
                    # readuntil leaves the buffer untouched on overrun, so
                    # e.consumed bytes are guaranteed buffered: if the newline was
                    # already buffered the next readuntil starts at the following
                    # valid line with nothing lost; if not, the line's remaining
                    # tail parses as non-JSON noise below and is skipped. (The
                    # previous readline()+feed_data resync could drop valid lines
                    # — readline clears the buffer before raising — and tripped
                    # "feed_data after feed_eof" when the process died mid-line.)
                    await stdout.readexactly(e.consumed)
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise; ignore
                for ev in self._translate(obj):
                    await self._events.put(ev)
            # Natural EOF: claude closed stdout on its own, so the conversation
            # cannot continue (a deliberate close() cancels this task before the
            # process is touched, so we never get here on normal shutdown).
            # `self._proc.returncode` is NOT authoritative yet — at pipe-EOF time
            # the exit may not have been reaped, so it reads None and a crash
            # would surface no event, leaving the orchestrator waiting forever.
            # wait() gets the real code; any spontaneous exit is fatal (review I6).
            rc = await self._proc.wait()
            await self._events.put(BackendError(f"claude exited rc={rc}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive: never let the reader die silently
            await self._events.put(BackendError(f"stdout reader: {exc!r}"))

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        stderr = self._proc.stderr
        try:
            while True:
                try:
                    raw = await stderr.readuntil(b"\n")
                except asyncio.IncompleteReadError as e:
                    raw = e.partial
                    if not raw:
                        break
                except asyncio.LimitOverrunError as e:
                    # Bound the stderr reader the same way as stdout (security
                    # H2): discard an over-long diagnostic line and keep draining.
                    await stderr.readexactly(e.consumed)
                    continue
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

        # Permission gate (--permission-prompt-tool stdio). Wire shape pinned
        # live in docs/permission-spike-results.md; the alternate spellings are
        # kept so a CLI version skew degrades to a prompt, not a crash. Other
        # control traffic (e.g. the ack for our own interrupt request) is
        # deliberately ignored.
        if t in ("control_request", "sdk_control_request"):
            req = obj.get("request") or {}
            if req.get("subtype") in ("can_use_tool", "permission"):
                return [
                    PermissionRequest(
                        request_id=str(
                            obj.get("request_id") or req.get("request_id") or ""
                        ),
                        tool_name=str(
                            req.get("tool_name") or req.get("tool") or "tool"
                        ),
                        tool_input=req.get("input") or req.get("tool_input") or {},
                    )
                ]

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
            elif delta.get("type") == "thinking_delta":
                # Extended-thinking stream: surfaced for the screen only —
                # never spoken, never in the turn rollup.
                thought = delta.get("thinking", "")
                if thought:
                    return [ThinkingDelta(text=thought)]
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
                # The stream path already announced this call by name (lowest
                # latency); this full-message copy carries the arguments, so it
                # goes out as an `upgrade` — detail, not a second call.
                name = block.get("name", "tool")
                inp = block.get("input") or {}
                out.append(
                    ToolActivity(
                        name=name,
                        summary=_summarize_tool_input(name, inp),
                        upgrade=True,
                        body=_tool_body(name, inp),
                    )
                )
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

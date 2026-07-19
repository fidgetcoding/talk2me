"""OpenAI Codex CLI backend — a different brain behind the same seams.

Codex has no long-lived bidirectional stream mode; it has something just as
good for a turn-taking voice loop: one `codex exec --json` process PER TURN,
with `codex exec resume <thread_id>` carrying the conversation between them.
Event schema pinned by live spike (codex-cli 0.144.6):

    {"type":"thread.started","thread_id":"..."}          -> session id
    {"type":"turn.started"}
    {"type":"item.started","item":{"type":"command_execution",
        "command":"...","status":"in_progress",...}}     -> tool activity
    {"type":"item.completed","item":{"type":"command_execution",
        "command":"...","aggregated_output":"...","exit_code":0,...}}
    {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
    {"type":"turn.completed","usage":{...}}              -> turn boundary

Prose arrives as COMPLETE messages (no deltas) — the orchestrator already
handles that shape (it's the partials-off Claude path). Safety comes from
Codex's own sandbox (`--sandbox workspace-write`: repo-scoped writes, no
network); the spoken approval gate doesn't apply — exec mode never asks.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable

from ..events import (
    AgentEvent,
    AssistantTextDelta,
    BackendError,
    ThinkingDelta,
    ToolActivity,
    TurnComplete,
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SUMMARY_MAX = 60
_BODY_MAX_CHARS = 2000


def _clean(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


class CodexBackend:
    """AgentBackend over `codex exec --json` / `codex exec resume`."""

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        model: str | None = None,
        cwd: str | None = None,
        resume_session_id: str | None = None,
        sandbox: str = "workspace-write",
        extra_args: list[str] | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> None:
        self._bin = codex_bin
        self._model = model
        self._cwd = cwd
        self._sandbox = sandbox
        self._extra_args = extra_args or []
        # thread_id doubles as the session id (resume keeps it; spike-pinned).
        self._session_id: str | None = resume_session_id
        self._on_session = on_session
        self._events: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._proc: asyncio.subprocess.Process | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_text: list[str] = []

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        pass  # per-turn processes; nothing lives between turns

    def _argv(self) -> list[str]:
        # Clap quirk (live-hit): --sandbox / -m / -C are PARENT flags and
        # must precede the `resume` subcommand; --json / --skip-git-repo-check
        # are accepted after it.
        argv = [self._bin, "exec", "--sandbox", self._sandbox]
        if self._model:
            argv += ["-m", self._model]
        if self._cwd:
            argv += ["-C", self._cwd]
        argv += self._extra_args
        if self._session_id:
            argv += ["resume", self._session_id]
        argv += ["--json", "--skip-git-repo-check", "-"]  # prompt on stdin
        return argv

    async def send(self, user_text: str) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            raise RuntimeError("previous turn still running")
        self._turn_text.clear()
        self._turn_task = asyncio.create_task(self._run_turn(user_text))

    async def _run_turn(self, user_text: str) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            await self._events.put(
                BackendError(f"codex unavailable: {exc}")
            )
            return
        proc = self._proc
        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(user_text.encode("utf-8"))
            proc.stdin.close()
            saw_turn_end = False
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for ev in self._translate(obj):
                    if isinstance(ev, TurnComplete):
                        saw_turn_end = True
                    await self._events.put(ev)
            rc = await proc.wait()
            if not saw_turn_end:
                if rc != 0:
                    await self._events.put(
                        BackendError(
                            f"codex exited rc={rc} (not logged in? run "
                            "`codex login`)"
                        )
                    )
                else:
                    # Interrupted or clipped turn: close it out cleanly.
                    await self._events.put(
                        TurnComplete(text="".join(self._turn_text).strip())
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never die silently
            await self._events.put(BackendError(f"codex turn: {exc!r}"))
        finally:
            if self._proc is proc:
                self._proc = None

    def _translate(self, obj: dict) -> list[AgentEvent]:
        t = obj.get("type")
        if t == "thread.started":
            tid = obj.get("thread_id")
            if tid and tid != self._session_id:
                self._session_id = tid
                if self._on_session is not None:
                    self._on_session(tid)
            return []
        if t in ("item.started", "item.completed", "item.updated"):
            return self._translate_item(
                obj.get("item") or {}, completed=t == "item.completed"
            )
        if t == "turn.completed":
            rollup = "".join(self._turn_text).strip()
            self._turn_text.clear()
            return [TurnComplete(text=rollup)]
        if t == "turn.failed" or t == "error":
            msg = _clean(str(obj.get("message") or obj.get("error") or obj))
            return [BackendError(f"codex: {msg[:200]}")]
        return []

    def _translate_item(self, item: dict, *, completed: bool) -> list[AgentEvent]:
        itype = item.get("type")
        if itype == "agent_message" and completed:
            text = _clean(item.get("text", ""))
            if not text:
                return []
            self._turn_text.append(text + " ")
            return [AssistantTextDelta(text=text)]
        if itype == "reasoning" and completed:
            thought = _clean(item.get("text", "") or item.get("summary", ""))
            return [ThinkingDelta(text=thought)] if thought else []
        if itype == "command_execution":
            cmd = " ".join(_clean(str(item.get("command", ""))).split())
            short = cmd[: _SUMMARY_MAX - 1] + "…" if len(cmd) > _SUMMARY_MAX else cmd
            if not completed:
                return [ToolActivity(name="shell", summary=short)]
            body = _clean(str(item.get("aggregated_output", "")))[:_BODY_MAX_CHARS]
            return [
                ToolActivity(
                    name="shell", summary=short, upgrade=True, body=body.strip()
                )
            ]
        if itype in ("file_change", "patch_apply") and completed:
            return [ToolActivity(name="edit", summary="", upgrade=False)]
        return []

    # ---- host controls ---------------------------------------------------

    def events(self) -> AsyncIterator[AgentEvent]:
        return self._event_iter()

    async def _event_iter(self) -> AsyncIterator[AgentEvent]:
        while True:
            yield await self._events.get()

    async def interrupt(self) -> None:
        """Kill the in-flight turn; the reader's fallback closes it out with
        a TurnComplete, mirroring the Claude interrupt flow."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    async def respond_permission(
        self, request_id: str, allow: bool, *, message: str | None = None
    ) -> None:
        pass  # exec mode never asks; the sandbox is the guardrail

    async def switch_session(self, resume_session_id: str) -> None:
        """Session picker support: the next turn resumes the chosen thread."""
        await self.interrupt()
        self._session_id = resume_session_id

    async def close(self) -> None:
        task, self._turn_task = self._turn_task, None
        await self.interrupt()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

"""Test doubles for the hardware/agent edges, so the loop runs headless.

The orchestrator depends only on Protocols, so swapping the real Mic/Speaker/
backend for these lets the entire turn loop run over SSH with no mic, no audio
device, and no LLM cost. This is the rig that makes "test later" mostly a myth:
everything but live capture + UX feel is exercised right here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from collections import deque

from talk2me.events import (
    AgentEvent,
    AssistantTextDelta,
    PermissionRequest,
    SessionReady,
    TurnComplete,
)


class FakeMic:
    """Replays a fixed list of frames, then ends the stream (clean shutdown).

    Frames are consumed from a shared deque, so multiple frames() iterators
    (main listen loop + the permission gate's temporary segmenter) share one
    stream position — mirroring the real Mic's single queue.
    """

    def __init__(self, frames: list[np.ndarray], sample_rate: int = 16000) -> None:
        self._frames = deque(frames)
        self.sample_rate = sample_rate
        self.muted_log: list[bool] = []
        self.muted = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.muted_log.append(muted)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        while self._frames:
            await asyncio.sleep(0)  # yield control like the real queue would
            if self.muted:
                # The real mic drops frames at the callback while muted; the
                # replay pauses instead (the "user" politely waits their turn),
                # so scripted utterances land when the mic can actually hear.
                continue
            try:
                yield self._frames.popleft()
            except IndexError:
                # A concurrent iterator (barge monitor + main loop share the
                # deque) drained the last frame between the check and the
                # pop — end this stream like the real queue just going quiet.
                return


class FakeSpeaker:
    """Records how many utterances it was asked to play."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate
        self.plays = 0

    def stop(self) -> None:
        pass

    async def play(self, blocks) -> bool:
        self.plays += 1
        async for _ in blocks:
            pass
        return True


class FakeSTT:
    """Returns a canned transcript per call, in order."""

    def __init__(self, transcripts: list[str]) -> None:
        self._t = list(transcripts)
        self.calls = 0

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        self.calls += 1
        return self._t.pop(0) if self._t else ""


class FakeTTS:
    sample_rate = 16000

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        self.spoken.append(text)
        yield np.zeros(256, dtype=np.float32)


class FakeBackend:
    """Scripts a reply per user turn: emits text deltas then TurnComplete.

    Two scripting styles:
    - `replies`: one text string per turn -> delta + TurnComplete (the classic).
    - `scripts`: one explicit event list per turn. A PermissionRequest in a
      script pauses the feed until respond_permission() is called, exactly like
      the real CLI blocking on a control_response.
    """

    def __init__(
        self,
        replies: list[str] | None = None,
        scripts: list[list[AgentEvent]] | None = None,
    ) -> None:
        self._replies = list(replies or [])
        self._scripts = list(scripts or [])
        self.sent: list[str] = []
        self.permission_responses: list[tuple[str, bool, str | None]] = []
        self.interrupts = 0
        self.started = False
        self.closed = False
        self._q: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._perm_gate = asyncio.Event()
        self._script_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.started = True
        await self._q.put(SessionReady(session_id="fake"))

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        if self._scripts:
            script = self._scripts.pop(0)
            # Feed in the background so a mid-script PermissionRequest can block
            # on the gate while the orchestrator consumes ahead of it.
            self._script_tasks.append(asyncio.create_task(self._feed(script)))
            return
        reply = self._replies.pop(0) if self._replies else "ok."
        await self._q.put(AssistantTextDelta(text=reply))
        await self._q.put(TurnComplete(text=reply))

    async def _feed(self, script: list[AgentEvent]) -> None:
        for ev in script:
            if isinstance(ev, PermissionRequest):
                self._perm_gate.clear()
                await self._q.put(ev)
                await self._perm_gate.wait()
            else:
                await self._q.put(ev)

    def events(self) -> AsyncIterator[AgentEvent]:
        async def it() -> AsyncIterator[AgentEvent]:
            while True:
                yield await self._q.get()

        return it()

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def respond_permission(
        self, request_id: str, allow: bool, *, message: str | None = None
    ) -> None:
        self.permission_responses.append((request_id, allow, message))
        self._perm_gate.set()

    async def close(self) -> None:
        for task in self._script_tasks:
            task.cancel()
        self.closed = True

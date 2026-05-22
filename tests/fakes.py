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

from talk2me.events import AgentEvent, AssistantTextDelta, SessionReady, TurnComplete


class FakeMic:
    """Replays a fixed list of frames, then ends the stream (clean shutdown)."""

    def __init__(self, frames: list[np.ndarray], sample_rate: int = 16000) -> None:
        self._frames = frames
        self.sample_rate = sample_rate
        self.muted_log: list[bool] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def set_muted(self, muted: bool) -> None:
        self.muted_log.append(muted)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        for f in self._frames:
            await asyncio.sleep(0)  # yield control like the real queue would
            yield f


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
    """Scripts a reply per user turn: emits text deltas then TurnComplete."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.sent: list[str] = []
        self.started = False
        self.closed = False
        self._q: asyncio.Queue[AgentEvent] = asyncio.Queue()

    async def start(self) -> None:
        self.started = True
        await self._q.put(SessionReady(session_id="fake"))

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        reply = self._replies.pop(0) if self._replies else "ok."
        await self._q.put(AssistantTextDelta(text=reply))
        await self._q.put(TurnComplete(text=reply))

    def events(self) -> AsyncIterator[AgentEvent]:
        async def it() -> AsyncIterator[AgentEvent]:
            while True:
                yield await self._q.get()

        return it()

    async def interrupt(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True

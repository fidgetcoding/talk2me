"""How long does the default voice stop between chunks? Mac only, measured on files.

The real orchestrator and real `say` render to temp files, and a fake speaker plays
them on a clock: it sleeps for each block's length and logs what would have been
audible when. Nothing reaches a speaker. Two numbers matter:

- stops: how long playback sat waiting because the next chunk wasn't rendered yet.
  That's dead air, on top of whatever pause the voice itself put there.
- seams: places the voice restarts, where intonation resets. Text rendered together
  gets `say`'s own pause and flows into the next sentence.

It runs a typical reply the old way (one render at a time, one per sentence) and the
new way (renders overlap, waiting sentences merge), at the speeds agents stream text.

Run:  python -m tests.manual_voice_gap_check
"""

import asyncio
import time

import numpy as np

from talk2me.config import Config
from talk2me.events import AssistantTextDelta, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.tts import SayTTS
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSTT

REPLY = (
    "I found the bug. The session token expires too early. It was set to one hour, "
    "but the refresh job only runs every two hours. So people get logged out. "
    "I changed the refresh to run every thirty minutes. The tests pass. "
    "I also added a test for the expired case. Want me to push it? "
    "Or should we look at the login page next? That one has a slow query too."
)


class ClockSpeaker:
    """Plays nothing. Keeps real time and logs when each chunk would be heard."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.spans: list[tuple[float, float]] = []

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def play(self, blocks) -> bool:
        start = time.monotonic()
        samples = 0
        async for block in blocks:
            samples += len(block)
        # Sleep to one clock so scheduling jitter can't pile up block by block.
        await asyncio.sleep(samples / self.sample_rate)
        self.spans.append((start, start + samples / self.sample_rate))
        return True


class StreamingBackend(FakeBackend):
    """Delivers the reply word by word at a set speed, like a real model."""

    def __init__(self, chars_per_second: float) -> None:
        super().__init__()
        self.cps = chars_per_second

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._script_tasks.append(asyncio.create_task(self._stream()))

    async def _stream(self) -> None:
        words = REPLY.split(" ")
        for i, word in enumerate(words):
            text = word if i == 0 else f" {word}"
            await self._q.put(AssistantTextDelta(text=text))
            await asyncio.sleep(len(text) / self.cps)
        await self._q.put(TurnComplete(text=REPLY))


class PlainSay(SayTTS):
    """The old pipeline: one sentence per render, and the next render starts when
    the last one ends. The lock stands in for rendering one at a time."""

    merge_queued_text = False
    _lock: asyncio.Lock | None = None

    async def synthesize(self, text):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            blocks = [block async for block in super().synthesize(text)]
        for block in blocks:
            yield block


async def _run(tts, cps: float) -> list[float]:
    """Dead air before each chunk after the first, in ms."""
    speaker = ClockSpeaker(tts.sample_rate)
    speech = (np.random.randn(480) * 0.2).astype(np.float32)
    frames = [speech] * 15 + [np.zeros(480, dtype=np.float32)] * 60
    orch = Orchestrator(
        cfg=Config(silence_ms=300, min_speech_ms=100),
        backend=StreamingBackend(cps),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=FakeSTT(["What was the bug?"]),
        tts=tts,
        mic=FakeMic(frames),
        speaker=speaker,
    )
    await asyncio.wait_for(orch.run(), timeout=180)
    return [
        max(0.0, (later[0] - earlier[1]) * 1000)
        for earlier, later in zip(speaker.spans, speaker.spans[1:])
    ]


def _line(label: str, stops: list[float]) -> str:
    return (
        f"{label}: {len(stops)} seams, {sum(stops) / 1000:.2f}s dead air, "
        f"worst stop {max(stops, default=0):.0f} ms, "
        f"{sum(s > 100 for s in stops)} stops over 100 ms"
    )


async def main() -> None:
    for voice in ("Samantha", "Ava (Premium)"):
        for name, cps in (("fast model", 300.0), ("slow model", 90.0)):
            for label, cls in (("before", PlainSay), ("after", SayTTS)):
                print(_line(f"{voice}, {name}, {label}", await _run(cls(voice=voice, rate_wpm=236), cps)), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

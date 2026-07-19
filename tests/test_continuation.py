"""Think-phase continuation stitching, headless: the user's turn SOUNDED
complete (so pre-send stitching let it through), the agent starts thinking,
and the user adds more before any answer is spoken — the monitor must
interrupt the agent and run() must stitch old + new into one instruction
(half-duplex, no --barge-in needed).

(The cut-off-mid-sentence case is handled earlier by pre-send stitching —
see tests/test_flow.py.)

Run:  ./.venv/bin/python -m tests.test_continuation
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.events import AssistantTextDelta, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

SR = 16000
FRAME = 480

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


class ThinkingBackend(FakeBackend):
    """Turn 1: thinks silently (emits nothing) until interrupted — the window
    where a real agent is planning/tool-calling before any speakable text.
    Turn 2 (the stitched instruction) answers normally."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            return  # thinking… TurnComplete arrives via interrupt()
        await self._q.put(AssistantTextDelta(text="The orchestrator runs the loop."))
        await self._q.put(TurnComplete(text="The orchestrator runs the loop."))

    async def interrupt(self) -> None:
        await super().interrupt()
        await self._q.put(TurnComplete(text=""))


async def main() -> int:
    cfg = Config(silence_ms=900, min_speech_ms=250)  # half-duplex default
    # Utterance 1 sounds complete (period -> sent straight through); utterance
    # 2 = the user adding more while the agent is still thinking.
    frames = _speech(15) + _silence(35) + _speech(20) + _silence(35)

    mic = FakeMic(frames, sample_rate=SR)
    stt = FakeSTT(["Count to fifty.", "Actually just count to ten."])
    tts = FakeTTS()
    backend = ThinkingBackend()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=stt,
        tts=tts,
        mic=mic,
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=10)

    check(
        "think-phase addition stitched onto the turn",
        backend.sent
        == [
            "Count to fifty.",
            "Count to fifty. Actually just count to ten.",
        ],
        str(backend.sent),
    )
    check("thinking turn was interrupted", backend.interrupts == 1)
    check(
        "stitched turn's reply spoken",
        "The orchestrator runs the loop." in tts.spoken,
        str(tts.spoken),
    )
    check("clean shutdown", backend.closed and not mic.started)

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

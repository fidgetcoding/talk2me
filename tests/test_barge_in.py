"""Full-duplex barge-in, headless. No mic, no audio device, no LLM cost.

Choreography: the user asks a question; while the agent is mid-answer the mic
stream carries fresh speech. The monitor must stop the Speaker, fire a REAL
backend interrupt (the fake ends the turn on it, mirroring the CLI's
error_during_execution result), transcribe the interruption WITH its onset
audio, and run() must send it as the immediate next turn.

Run:  ./.venv/bin/python -m tests.test_barge_in
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.events import AssistantTextDelta, ToolActivity, TurnComplete
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


class BargeBackend(FakeBackend):
    """Turn 1 streams deltas but only completes when interrupted (like the real
    CLI ending an interrupted turn with error_during_execution -> TurnComplete).
    Turn 2 (the barge text) gets a normal scripted reply."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            await self._q.put(AssistantTextDelta(text="Let me explain at length. "))
            await self._q.put(AssistantTextDelta(text="There are many details. "))
            # No TurnComplete — it arrives via interrupt(), below.
        else:
            await self._q.put(AssistantTextDelta(text="Okay, stopping."))
            await self._q.put(TurnComplete(text="Okay, stopping."))

    async def interrupt(self) -> None:
        await super().interrupt()
        await self._q.put(TurnComplete(text="Let me explain at length."))


class StoppableSpeaker(FakeSpeaker):
    def __init__(self, sample_rate: int = 16000) -> None:
        super().__init__(sample_rate)
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class PauseBargeBackend(FakeBackend):
    """Turn 1 runs a tool and streams prose, completing only on interrupt.
    Turn 2 (the automatic task-resume) completes normally."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            await self._q.put(ToolActivity(name="Write"))
            await self._q.put(AssistantTextDelta(text="Building it now. "))
        else:
            await self._q.put(AssistantTextDelta(text="All done."))
            await self._q.put(TurnComplete(text="All done."))

    async def interrupt(self) -> None:
        await super().interrupt()
        await self._q.put(TurnComplete(text="Building it now."))


async def pause_mid_work_case() -> None:
    """'Sleep.' spoken while the agent works: ears pause, the control word
    never reaches the agent, and the interrupted task is resent."""
    cfg = Config(silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False)
    frames = _speech(15) + _silence(80) + _speech(20) + _silence(35)
    mic = FakeMic(frames, sample_rate=SR)
    backend = PauseBargeBackend()
    tts = FakeTTS()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Build the pong game.", "Sleep."]),
        tts=tts,
        mic=mic,
        speaker=StoppableSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=10)

    check(
        "pause-barge: control word never sent, task resent instead",
        backend.sent == ["Build the pong game.", "Build the pong game."],
        str(backend.sent),
    )
    check("pause-barge: turn was interrupted once", backend.interrupts == 1)
    check("pause-barge: ears ended paused", orch._paused is True)
    check(
        "pause-barge: pause confirmation spoken",
        any("Paused" in s for s in tts.spoken),
        str(tts.spoken),
    )
    check(
        "pause-barge: resumed task completed",
        any("All done." in s for s in tts.spoken),
        str(tts.spoken),
    )


async def main() -> int:
    cfg = Config(silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False)
    # Utterance 1 = the question. Then, while the agent "speaks", utterance 2 =
    # the interruption. Monitor onset fires after ~8 voiced frames (250ms/30ms).
    # The extra silence gap keeps the (fast, frame-per-loop-tick) monitor from
    # reaching utterance 2's onset before the fakes have spoken both sentences
    # — mirroring real time, where speech playback far outlasts a few frames.
    frames = _speech(15) + _silence(80) + _speech(15) + _silence(35)

    mic = FakeMic(frames, sample_rate=SR)
    speaker = StoppableSpeaker(SR)
    stt = FakeSTT(["Explain quantum computing.", "actually stop"])
    tts = FakeTTS()
    backend = BargeBackend()
    vad = EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012)

    orch = Orchestrator(
        cfg=cfg, backend=backend, vad=vad, stt=stt, tts=tts, mic=mic, speaker=speaker
    )
    await asyncio.wait_for(orch.run(), timeout=10)

    check(
        "both turns sent, barge text second",
        backend.sent == ["Explain quantum computing.", "actually stop"],
        str(backend.sent),
    )
    check("backend interrupted exactly once", backend.interrupts == 1)
    check("speaker playback was cut", speaker.stops >= 1, f"stops={speaker.stops}")
    check(
        "reply to the barge turn was spoken",
        "Okay, stopping." in tts.spoken,
        str(tts.spoken),
    )
    check(
        "mic never muted in full duplex",
        True not in mic.muted_log,
        str(mic.muted_log),
    )
    check("clean shutdown", backend.closed and not mic.started)

    await pause_mid_work_case()

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

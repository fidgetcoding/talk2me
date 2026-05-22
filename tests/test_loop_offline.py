"""Full orchestrator turn loop, headless. No mic, no audio device, no LLM cost.

FakeMic replays one synthetic utterance, FakeSTT canned-transcribes it,
FakeBackend scripts a reply, FakeTTS/FakeSpeaker capture playback. Asserts the
loop transcribed -> sent the right text -> spoke the reply -> muted then
unmuted the mic (half-duplex contract).

Run:  ./.venv/bin/python -m tests.test_loop_offline
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.orchestrator import Orchestrator
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

SR = 16000
FRAME = 480


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


async def main() -> int:
    cfg = Config(silence_ms=900, min_speech_ms=250)
    frames = _speech(15) + _silence(35)  # one utterance, then stream ends -> shutdown

    mic = FakeMic(frames, sample_rate=SR)
    speaker = FakeSpeaker(SR)
    stt = FakeSTT(["what is two plus two"])
    tts = FakeTTS()
    backend = FakeBackend(["Two plus two is four. Anything else?"])
    vad = EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012)

    orch = Orchestrator(
        cfg=cfg, backend=backend, vad=vad, stt=stt, tts=tts, mic=mic, speaker=speaker
    )
    await asyncio.wait_for(orch.run(), timeout=10)

    transcribed = stt.calls == 1
    sent_right = backend.sent == ["what is two plus two"]
    # two sentences -> two TTS chunks spoken
    spoke = tts.spoken == ["Two plus two is four.", "Anything else?"]
    muted_then_unmuted = mic.muted_log[:1] == [True] and mic.muted_log[-1:] == [False]
    closed = backend.closed and not mic.started

    ok = transcribed and sent_right and spoke and muted_then_unmuted and closed
    print(f"transcribed={transcribed} sent={backend.sent} spoken={tts.spoken}")
    print(f"mute_log={mic.muted_log} closed={closed} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

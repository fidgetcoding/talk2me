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
    # TWO utterances back to back -> proves the events generator resumes across
    # turns and mute/unmute cycles correctly. Then the stream ends -> shutdown.
    frames = _speech(15) + _silence(35) + _speech(15) + _silence(35)

    mic = FakeMic(frames, sample_rate=SR)
    speaker = FakeSpeaker(SR)
    # Punctuated like real engines: whisper/parakeet close complete sentences,
    # and the pre-send continuation heuristic keys off exactly that.
    stt = FakeSTT(["What is two plus two?", "Thanks."])
    tts = FakeTTS()
    backend = FakeBackend(["Two plus two is four. Anything else?", "You're welcome."])
    vad = EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012)

    orch = Orchestrator(
        cfg=cfg, backend=backend, vad=vad, stt=stt, tts=tts, mic=mic, speaker=speaker
    )
    await asyncio.wait_for(orch.run(), timeout=10)

    two_turns = stt.calls == 2
    sent_right = backend.sent == ["What is two plus two?", "Thanks."]
    spoke = tts.spoken == [
        "Two plus two is four.",
        "Anything else?",
        "You're welcome.",
    ]
    # mute on each turn's first speech, unmute at each turn's end: T,F,T,F
    mute_cycled = mic.muted_log == [True, False, True, False]
    closed = backend.closed and not mic.started

    ok = two_turns and sent_right and spoke and mute_cycled and closed
    print(f"turns={stt.calls} sent={backend.sent}")
    print(f"spoken={tts.spoken}")
    print(f"mute_log={mic.muted_log} closed={closed} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

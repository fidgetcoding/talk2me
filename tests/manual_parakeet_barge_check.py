"""Manual check: the full barge-in flow with the REAL Parakeet engine.

Everything is headless except the STT: real MLX model, real audio (a `say`-
rendered utterance chunked into mic-sized frames), the true warmup-before-mic
path, and the monitor's transcribe on the dedicated MLX thread. This is the
whole --barge-in --stt parakeet stack minus the physical microphone.

Not in CI (needs the downloaded model + macOS `say`).

Run:  ./.venv/bin/python -m tests.manual_parakeet_barge_check
"""

import asyncio
import subprocess
import tempfile
import time
import wave

import numpy as np

from talk2me.config import Config
from talk2me.events import AssistantTextDelta, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.stt.parakeet import ParakeetMLXSTT
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeTTS

SR = 16000
FRAME = 480


def _spoken_frames(text: str) -> list[np.ndarray]:
    """Render `text` with `say` and chunk it into mic-sized float32 frames."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    subprocess.run(
        ["say", "-o", path, "--data-format=LEI16@16000", "--", text],
        check=True,
        capture_output=True,
    )
    with wave.open(path) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    # Boost so the energy VAD (threshold 0.012 RMS) reads it as speech.
    peak = float(np.abs(audio).max() or 1.0)
    audio = audio * (0.5 / peak)
    n = (audio.shape[0] // FRAME) * FRAME
    return [audio[i : i + FRAME] for i in range(0, n, FRAME)]


def _silence(n: int) -> list[np.ndarray]:
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


class BargeBackend(FakeBackend):
    """Turn 1 streams text and completes only on interrupt; turn 2 is normal."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            await self._q.put(AssistantTextDelta(text="One, two, three, four. "))
            await self._q.put(AssistantTextDelta(text="Five, six, seven, eight. "))
            return  # keeps "speaking" until interrupted
        await self._q.put(AssistantTextDelta(text="Hello!"))
        await self._q.put(TurnComplete(text="Hello!"))

    async def interrupt(self) -> None:
        await super().interrupt()
        await self._q.put(TurnComplete(text=""))


async def main() -> int:
    question = _spoken_frames("What is two plus two?")
    interruption = _spoken_frames("Say hello instead")
    # Long silence gap ≈ playback time in the fakes' compressed clock; then the
    # interruption arrives "mid-answer".
    frames = question + _silence(80) + interruption + _silence(45)

    cfg = Config(silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False)
    stt = ParakeetMLXSTT()
    backend = BargeBackend()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=stt,
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )
    t0 = time.time()
    await asyncio.wait_for(orch.run(), timeout=120)
    elapsed = time.time() - t0

    sent = backend.sent
    ok = (
        len(sent) == 2
        and "2" in sent[0].replace("two", "2").lower()
        and "hello" in sent[1].lower()
        and backend.interrupts == 1
    )
    print(f"\nsent={sent}")
    print(f"interrupts={backend.interrupts} elapsed={elapsed:.1f}s")
    print(f"-> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

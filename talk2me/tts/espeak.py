"""espeak-ng TTS: the free, always-available voice for Linux (and anywhere it's
installed). It sounds like a robot, because it is one, but it's one apt-get away and
it beats the silence you'd get from `say` not existing.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import wave
from collections.abc import AsyncIterator

import numpy as np

_BLOCK = 2048
SAMPLE_RATE = 22050  # espeak-ng's fixed output rate


def available() -> bool:
    return shutil.which("espeak-ng") is not None


class EspeakTTS:
    sample_rate: int = SAMPLE_RATE

    def __init__(self, *, voice: str | None = None, rate_wpm: int | None = None) -> None:
        self._voice = voice
        self._rate_wpm = rate_wpm

    def _render(self, text: str) -> np.ndarray | None:
        argv = ["espeak-ng", "--stdout"]
        if self._voice:
            argv += ["-v", self._voice]
        if self._rate_wpm:
            argv += ["-s", str(self._rate_wpm)]
        argv += ["--", text]
        try:
            out = subprocess.run(argv, check=True, capture_output=True).stdout
            with wave.open(io.BytesIO(out), "rb") as wf:
                raw = wf.readframes(wf.getnframes())
        except (subprocess.CalledProcessError, OSError, wave.Error, EOFError):
            return None  # same deal as `say`: a bad render is silence, not a crash
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = text.strip()
        if not text:
            return
        pcm = await asyncio.to_thread(self._render, text)
        if pcm is None:
            return
        for i in range(0, pcm.shape[0], _BLOCK):
            yield pcm[i : i + _BLOCK]

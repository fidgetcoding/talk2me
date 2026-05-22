"""macOS `say` TTS. Synthesizes to a temp WAV, then yields PCM blocks.

`say` is free, offline, and always present on macOS. We render the whole chunk
to a temp file (LEI16 @ 16 kHz mono), then stream it back in small blocks so the
Speaker can stop mid-utterance for barge-in. Swap in ElevenLabs/Kitten later by
satisfying the same TTS Protocol — true streaming synthesis, no file hop.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from collections.abc import AsyncIterator

import numpy as np

_BLOCK = 2048  # samples per yielded block (~128 ms @ 16 kHz)


class SayTTS:
    sample_rate: int = 16000

    def __init__(self, *, voice: str | None = None, rate_wpm: int | None = None) -> None:
        self._voice = voice
        self._rate_wpm = rate_wpm

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = text.strip()
        if not text:
            return
        path = await asyncio.to_thread(self._render, text)
        try:
            for block in self._read_blocks(path):
                yield block
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _render(self, text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="t2m_say_")
        os.close(fd)
        argv = ["say", "-o", path, "--data-format=LEI16@16000"]
        if self._voice:
            argv += ["-v", self._voice]
        if self._rate_wpm:
            argv += ["-r", str(self._rate_wpm)]
        argv += ["--", text]
        # Blocking subprocess in a worker thread (we're already off the loop).
        import subprocess

        subprocess.run(argv, check=True, capture_output=True)
        return path

    def _read_blocks(self, path: str):
        with wave.open(path, "rb") as wf:
            n = wf.getnframes()
            raw = wf.readframes(n)
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        for i in range(0, pcm.shape[0], _BLOCK):
            yield pcm[i : i + _BLOCK]

"""Parakeet STT via MLX — Apple Silicon GPU transcription.

NVIDIA Parakeet TDT 0.6B run through parakeet-mlx: more accurate than any
local Whisper size AND an order of magnitude faster on M-series, because it
uses the GPU (faster-whisper's CTranslate2 runtime is CPU-only on Mac — no
Metal backend exists). Measured on this machine: ~0.08s per warm utterance
vs ~1s+ for whisper base.en. See docs/stt-upgrade-research.md rec #2.

Costs vs whisper: ~2 GB RAM while loaded, English-only (tdt-0.6b-v2), one
pure-MLX dependency (`pip install talk2me[parakeet]` — no torch, no NeMo),
and NO hotword biasing — the TDT decoder has no vocab-bias input, so
--vocab / live context seeding are whisper-only. Parakeet compensates with
raw accuracy.

The public parakeet-mlx transcribe() API is file-path-only, so utterances
are bridged through a temp WAV (2-10 s of 16-bit mono ≈ 64-320 KB — noise
next to model inference; parakeet resamples internally, so the capture rate
is written as-is).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor

import numpy as np


class ParakeetMLXSTT:
    """Transcribes complete utterances with Parakeet TDT on the M-series GPU.

    All MLX work (model load AND inference) is pinned to one dedicated worker
    thread: MLX streams are per-thread, so a model loaded on one thread raises
    "There is no Stream(cpu, 1) in current thread" when invoked from another —
    which is exactly what a shared asyncio.to_thread pool does.
    """

    def __init__(self, *, model: str = "mlx-community/parakeet-tdt-0.6b-v2") -> None:
        self._model_name = model
        self._model = None  # lazy — importing this module never loads MLX
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="parakeet-mlx"
        )

    def _ensure_model(self):
        # Only ever runs on the single executor thread, so no lock is needed
        # and load + inference always share one MLX stream.
        if self._model is None:
            from parakeet_mlx import from_pretrained

            self._model = from_pretrained(self._model_name)
        return self._model

    def warmup(self) -> None:
        """Blocking model load on the MLX worker thread, callable at startup so
        the first utterance doesn't pay it. Duck-typed (not part of the STT
        Protocol)."""
        self._executor.submit(self._ensure_model).result()

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._transcribe_sync, audio, sample_rate
        )

    def _transcribe_sync(self, audio: np.ndarray, sample_rate: int) -> str:
        model = self._ensure_model()
        pcm16 = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype(np.int16)
        fd, path = tempfile.mkstemp(prefix="talk2me-utt-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm16.tobytes())
            result = model.transcribe(path)
            return (result.text or "").strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

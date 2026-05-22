"""faster-whisper STT. Local, no network, no per-minute cost.

Model load is lazy (first transcribe) so importing the module is cheap and a
`--text` run never touches whisper. Transcription is blocking C++; we run it in
a thread so the audio event loop keeps breathing.
"""

from __future__ import annotations

import asyncio

import numpy as np


class WhisperSTT:
    """Transcribes complete utterances with faster-whisper."""

    def __init__(
        self,
        *,
        model: str = "base.en",
        device: str = "auto",
        compute_type: str = "int8",
        initial_prompt: str | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._initial_prompt = initial_prompt
        self._model = None  # lazy

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio, sample_rate)

    def _transcribe_sync(self, audio: np.ndarray, sample_rate: int) -> str:
        model = self._ensure_model()
        # faster-whisper wants float32 mono at 16 kHz. We capture at 16 kHz, so
        # no resample needed; guard anyway.
        if sample_rate != 16000:
            audio = _resample_to_16k(audio, sample_rate)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        segments, _info = model.transcribe(
            audio,
            language="en",
            initial_prompt=self._initial_prompt,
            vad_filter=False,  # we already segmented upstream
        )
        return "".join(seg.text for seg in segments).strip()


def _resample_to_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    # Linear resample — adequate for speech STT input.
    n_out = int(round(audio.shape[0] * 16000 / sample_rate))
    if n_out <= 0:
        return audio.astype(np.float32)
    x_old = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)

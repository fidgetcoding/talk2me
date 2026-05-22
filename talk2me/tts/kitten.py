"""KittenTTS — lightweight local neural TTS. Renders a chunk, then yields PCM.

KittenTTS (kitten-tts / KittenML) is a tiny CPU-friendly neural voice model. We
synthesize the whole chunk into one float32 waveform, then stream it back in
small blocks so the Speaker can stop mid-utterance for barge-in — mirroring
SayTTS's chunking, but with on-device neural synthesis instead of a file hop.

The `kittentts` package is heavy and optional, so we lazy-import it inside
`__init__` / `synthesize`: importing this module never fails even when the lib
is absent. The model exposes 24 kHz audio, surfaced as `self.sample_rate`.

factory.py build_tts branch:
    if cfg.tts == "kitten":
        return KittenTTS(voice=cfg.tts_voice)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

_BLOCK = 2048  # samples per yielded block (~85 ms @ 24 kHz)
_DEFAULT_MODEL = "KittenML/kitten-tts-nano-0.1"
_PIP_HINT = "pip install kittentts"


class KittenTTS:
    # KittenTTS renders 24 kHz mono audio.
    sample_rate: int = 24000

    def __init__(self, *, voice: str | None = None, model: str | None = None) -> None:
        self._voice = voice
        self._model_id = model or _DEFAULT_MODEL
        self._model = self._load_model()

    def _load_model(self) -> object:
        """Instantiate the KittenTTS model; raise a clear error if the lib is missing."""
        try:
            from kittentts import KittenTTS as _KittenTTS  # lazy: keep import optional
        except ImportError as exc:  # pragma: no cover  (depends on env)
            raise RuntimeError(
                f"KittenTTS requires the 'kittentts' package — install with `{_PIP_HINT}`"
            ) from exc
        return _KittenTTS(self._model_id)

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = text.strip()
        if not text:
            return
        pcm = await asyncio.to_thread(self._render, text)
        for i in range(0, pcm.shape[0], _BLOCK):
            yield pcm[i : i + _BLOCK]

    def _render(self, text: str) -> np.ndarray:
        """Synthesize `text` to a float32 mono waveform (blocking; runs off-loop)."""
        kwargs: dict[str, str] = {}
        if self._voice:
            kwargs["voice"] = self._voice
        audio = self._model.generate(text, **kwargs)
        pcm = np.asarray(audio, dtype=np.float32)
        return pcm.reshape(-1)

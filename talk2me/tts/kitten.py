"""KittenTTS — lightweight local neural TTS. Renders a chunk, then yields PCM.

KittenTTS (kitten-tts / KittenML) is a tiny CPU-friendly neural voice model. We
synthesize the whole chunk into one float32 waveform, then stream it back in
small blocks so the Speaker can stop mid-utterance for barge-in — mirroring
SayTTS's chunking, but with on-device neural synthesis instead of a file hop.

The `kittentts` package is heavy and optional, so we lazy-import it AND lazy-load
the model on first `synthesize()` — mirroring WhisperSTT / SileroVAD, which the
factory builds cheaply and only pay model-load cost on first use. `__init__` does
no I/O and never touches `kittentts`, so `import talk2me.tts.kitten` and
constructing `KittenTTS(...)` both stay free even when the lib is absent; the
RuntimeError surfaces only when synthesis is actually attempted. The model
exposes 24 kHz audio, surfaced as `self.sample_rate`.

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
        # Lazy: stays None until the first synthesize() call (see _ensure_model).
        # Construction is cheap and never imports the heavy `kittentts` lib.
        self._model: object | None = None

    def _ensure_model(self) -> object:
        """Load the KittenTTS model on first use; raise clearly if the lib is missing.

        Cached after the first call so subsequent renders reuse the instance.
        """
        if self._model is None:
            try:
                from kittentts import KittenTTS as _KittenTTS  # lazy: keep import optional
            except ImportError as exc:  # pragma: no cover  (depends on env)
                raise RuntimeError(
                    f"KittenTTS requires the 'kittentts' package — install with `{_PIP_HINT}`"
                ) from exc
            self._model = _KittenTTS(self._model_id)
        return self._model

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = text.strip()
        if not text:
            return
        pcm = await asyncio.to_thread(self._render, text)
        for i in range(0, pcm.shape[0], _BLOCK):
            yield pcm[i : i + _BLOCK]

    def _render(self, text: str) -> np.ndarray:
        """Synthesize `text` to a float32 mono waveform (blocking; runs off-loop)."""
        model = self._ensure_model()
        kwargs: dict[str, str] = {}
        if self._voice:
            kwargs["voice"] = self._voice
        audio = model.generate(text, **kwargs)
        pcm = np.asarray(audio, dtype=np.float32)
        return pcm.reshape(-1)

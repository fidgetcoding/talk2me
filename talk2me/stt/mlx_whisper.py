"""Whisper large-v3-turbo on the Apple-Silicon GPU, through mlx-whisper.

Opt-in (`--stt whisper-gpu`, `pip install talk2me[whisper-gpu]`); base.en on the
CPU stays the default. The big Whisper at conversational speed: on CPU
large-v3-turbo took ~6 s per utterance here, through MLX ~0.55 s, against 0.46 s
for base.en. Numbers and their limits are in the README ("Switching the ears").

Costs vs base.en: a ~1.6 GB model download once, more RAM while loaded, and a
heavy install (mlx-whisper's metadata pulls in torch, which it never imports).

Vocab differs from the CPU engine. mlx-whisper has no `hotwords`, so --vocab and
live context ride `initial_prompt`, the closest thing it offers. That's a softer
bias than hotwords, and it only conditions the first 30 s window of an utterance.
There's also no beam search here (greedy decode).

Silence needs its own guard. This model's no-speech head read 0.0 on silence,
noise, clicks and hum when measured, so mlx-whisper's built-in no-speech test
never fires: left alone it answers silence with "Thank you." or recites its own
prompt. Every utterance passes the Silero VAD that faster-whisper bundles first,
the same second opinion WhisperSTT gets from vad_filter=True (and it holds with
--no-speech-check too).

If this engine can't come up (broken import, no network for the first download,
a Metal error mid-session), it hands off to the `fallback` it was built with and
says so once, rather than taking the session down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .whisper import _MAX_CONTEXT_TERMS, _resample_to_16k

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

# Always in the prompt. Whisper copies a prompt's style, and without a punctuated
# sentence to copy, 3 of 24 test clips came back with no closing period, which
# the unfinished-sentence hold reads as a trailing-off speaker.
_BASE_PROMPT = "A developer is talking to a coding agent."

# Ceiling on the prompt. mlx-whisper keeps only the TAIL past 223 tokens, which
# would silently eat the static vocab; ~600 chars stays well under that.
_MAX_PROMPT_CHARS = 600


class MLXWhisperSTT:
    """Transcribes complete utterances with Whisper on the M-series GPU.

    All MLX work (import, model load, inference) is pinned to one dedicated
    worker thread, for the same reason as ParakeetMLXSTT: MLX streams are
    per-thread, so a model loaded on one thread can't run on another.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        vocab: list[str] | None = None,
        language: str = "en",
        fallback: Callable[[], object] | None = None,
    ) -> None:
        self._model_name = model
        self._vocab = list(vocab or [])
        # "auto" (or empty) detects per utterance; turbo is multilingual.
        self._language: str | None = None if language in ("auto", "") else language
        self._make_fallback = fallback
        self._fallback = None
        self._context_terms: list[str] = []
        self._mlx = None  # lazy: importing this module never loads MLX
        self._vad = None
        self._model_path: str | None = None
        self._notify: Callable[[str], None] = lambda _text: None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mlx-whisper"
        )

    @property
    def engine_name(self) -> str:
        """What's really transcribing, for the startup card. Duck-typed."""
        return "whisper" if self._fallback is not None else "whisper-gpu"

    def set_notifier(self, notify: Callable[[str], None]) -> None:
        """Where one-line user notices go (download, fallback). Duck-typed."""
        self._notify = notify

    def set_context(self, terms: list[str]) -> None:
        """Replace the live context terms. Duck-typed, same as WhisperSTT."""
        self._context_terms = list(terms)[:_MAX_CONTEXT_TERMS]
        if self._fallback is not None and hasattr(self._fallback, "set_context"):
            self._fallback.set_context(self._context_terms)

    def _initial_prompt(self) -> str:
        # Static vocab first, then live context, deduped; stop at the ceiling.
        seen: set[str] = set()
        terms: list[str] = []
        length = 0
        for term in self._vocab + self._context_terms:
            key = term.lower()
            if key in seen or length + len(term) > _MAX_PROMPT_CHARS:
                continue
            seen.add(key)
            terms.append(term)
            length += len(term) + 2
        return f"{_BASE_PROMPT} Terms: {', '.join(terms)}." if terms else _BASE_PROMPT

    def _resolve_model(self) -> str:
        # Offline-first: a cached model never touches the network, so launches
        # can't stall on a Hub check and work on a plane. Weights are checked
        # by name because an interrupted download leaves a snapshot without them.
        from huggingface_hub import snapshot_download

        try:
            path = Path(snapshot_download(self._model_name, local_files_only=True))
        except Exception:  # noqa: BLE001 — anything here just means "not cached"
            path = None
        if path and ((path / "weights.safetensors").exists() or (path / "weights.npz").exists()):
            return str(path)
        self._notify(
            "first run: downloading Whisper large-v3-turbo, about 1.6 GB, one "
            "time only. It isn't hung, it's just big."
        )
        return snapshot_download(self._model_name)

    def _ensure_model(self):
        # Only ever runs on the single executor thread, so no lock is needed.
        if self._mlx is None:
            path = self._resolve_model()
            import mlx_whisper
            from faster_whisper import vad

            self._vad = vad
            # A short silent decode, so the first real utterance pays neither
            # the weight load nor the first-graph cost.
            mlx_whisper.transcribe(
                np.zeros(8000, dtype=np.float32),
                path_or_hf_repo=path,
                language=self._language or "en",
                temperature=0.0,
                condition_on_previous_text=False,
            )
            self._model_path = path
            self._mlx = mlx_whisper
        return self._mlx

    def _switch_to_fallback(self, exc: Exception) -> None:
        self._fallback = self._make_fallback()
        if hasattr(self._fallback, "set_context"):
            self._fallback.set_context(self._context_terms)
        self._notify(
            f"whisper-gpu failed ({type(exc).__name__}: {str(exc)[:80]}), using "
            "whisper on the CPU instead"
        )

    def warmup(self) -> None:
        """Blocking load on the MLX worker thread, callable at startup so the
        first utterance doesn't pay it. Duck-typed (not part of the Protocol)."""
        try:
            self._executor.submit(self._ensure_model).result()
        except Exception as exc:
            if self._make_fallback is None:
                raise
            self._switch_to_fallback(exc)
            if hasattr(self._fallback, "warmup"):
                self._fallback.warmup()

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if self._fallback is None:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(
                    self._executor, self._transcribe_sync, audio, sample_rate
                )
            except Exception as exc:
                if self._make_fallback is None:
                    raise
                self._switch_to_fallback(exc)
        return await self._fallback.transcribe(audio, sample_rate)

    def _transcribe_sync(self, audio: np.ndarray, sample_rate: int) -> str:
        mlx_whisper = self._ensure_model()
        if sample_rate != 16000:
            audio = _resample_to_16k(audio, sample_rate)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if not self._vad.get_speech_timestamps(audio):
            return ""
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self._model_path,
            language=self._language,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=self._initial_prompt(),
        )
        return (result.get("text") or "").strip()

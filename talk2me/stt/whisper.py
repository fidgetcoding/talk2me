"""faster-whisper STT. Local, no network, no per-minute cost.

Model load is lazy (first transcribe) so importing the module is cheap and a
`--text` run never touches whisper. Transcription is blocking C++; we run it in
a thread so the audio event loop keeps breathing.

Term-accuracy levers (see docs/stt-upgrade-research.md): vocabulary is passed
as faster-whisper `hotwords` — which bias the WHOLE utterance — rather than
`initial_prompt`, which only conditions the first segment (and would override
hotwords if both were set). Decode is pinned to temperature=0.0 / beam_size=5
for dictation consistency. `set_context()` lets the orchestrator feed proper
nouns from the agent's latest reply, so terms the user is about to say back
are biased for — a local, conversation-scoped version of Wispr-style context
awareness.
"""

from __future__ import annotations

import asyncio

import numpy as np

# Ceiling on live context terms merged into hotwords. Keeps the bias string
# short — a huge hotword list dilutes the boost each term gets.
_MAX_CONTEXT_TERMS = 30


class WhisperSTT:
    """Transcribes complete utterances with faster-whisper."""

    def __init__(
        self,
        *,
        model: str = "base.en",
        device: str = "auto",
        compute_type: str = "int8",
        vocab: list[str] | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._vocab = list(vocab or [])
        self._context_terms: list[str] = []
        self._model = None  # lazy

    def set_context(self, terms: list[str]) -> None:
        """Replace the live context terms (proper nouns from the conversation).

        Called by the orchestrator after each agent turn; duck-typed, not part
        of the STT Protocol, so other engines simply don't receive context.
        """
        self._context_terms = list(terms)[:_MAX_CONTEXT_TERMS]

    def _hotwords(self) -> str | None:
        # Static vocab first (never truncated), then live context terms.
        seen: set[str] = set()
        terms: list[str] = []
        for term in self._vocab + self._context_terms:
            key = term.lower()
            if key not in seen:
                seen.add(key)
                terms.append(term)
        return ", ".join(terms) if terms else None

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
            hotwords=self._hotwords(),
            beam_size=5,
            temperature=0.0,
            condition_on_previous_text=False,
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

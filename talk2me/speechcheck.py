"""Silero speech confirmation — "was that actually a human talking?"

The capture VADs (energy, webrtc) answer "is there sound energy shaped
vaguely like voice?" and are fooled by typing, taps, coughs, and chair
squeaks. Silero is a trained speech classifier and is not. This module wraps
the Silero model that faster-whisper already bundles (zero new dependencies)
as a yes/no gate used in two places:

- the barge-in monitor, BEFORE it cuts the agent's turn — noise must never
  kill generation;
- the main loop, BEFORE an utterance is transcribed — noise dies silently
  instead of becoming a hallucinated message.

The gate is injected (Orchestrator ``speech_check=``), so headless tests —
whose synthetic "speech" is random noise that Silero would rightly reject —
run ungated unless a test injects its own.
"""

from __future__ import annotations

import numpy as np

# Total Silero-detected speech required to call a buffer "speech". Real words
# clear this trivially (a single vowel sustains longer); typing bursts and
# taps almost never do. Coughs are genuinely vocal and sometimes pass — the
# transcription-side gates (vad_filter + no-speech head) are their backstop.
MIN_SPEECH_MS = 200


class SileroSpeechCheck:
    """Callable gate: (audio, sample_rate) -> bool. Thread-safe after warmup.

    Optional third stage: voice-lock. When a VoiceLock with a loaded
    voiceprint is attached AND `locked` is True (solo session), audio must
    also BE the enrolled speaker. "team session" flips `locked` off live —
    everyone talks — without touching the noise gates."""

    def __init__(self, voicelock=None) -> None:
        self.voicelock = voicelock
        self.locked = voicelock is not None
        self.last_score: float | None = None

    def set_locked(self, locked: bool) -> None:
        self.locked = locked and self.voicelock is not None

    def __call__(self, audio: np.ndarray, sample_rate: int) -> bool:
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        # Reset per call: last_score is non-None ONLY when this rejection
        # (or acceptance) came from the voice-lock stage — the orchestrator
        # keys its "is the lock doubting its owner?" hint on that.
        self.last_score = None

        if sample_rate != 16000:
            audio = _resample_to_16k(audio, sample_rate)
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        # speech_pad_ms=0: the default pads every region by 400ms, which would
        # count silence around a click as "speech". We want honest durations.
        opts = VadOptions(
            min_speech_duration_ms=60,
            min_silence_duration_ms=300,
            speech_pad_ms=0,
        )
        regions = get_speech_timestamps(audio, opts, sampling_rate=16000)
        speech_ms = sum(r["end"] - r["start"] for r in regions) / 16.0
        if speech_ms < MIN_SPEECH_MS:
            return False
        if self.locked and self.voicelock is not None:
            ok, score = self.voicelock.verify(audio, sample_rate)
            self.last_score = score
            return ok
        return True

    def warmup(self) -> None:
        """Load the ONNX models off the hot path (first call pays ~100ms)."""
        self(np.zeros(3200, dtype=np.float32), 16000)
        if self.voicelock is not None:
            self.voicelock.warmup()


def build_speech_check(
    enabled: bool = True, voice_lock: bool = False
) -> SileroSpeechCheck | None:
    """The production gate, or None when disabled/unavailable — a missing
    classifier must degrade to v2.0.x behavior, never block the loop. The
    voice-lock stage attaches only when requested AND an enrolled voiceprint
    exists AND its model loads — any failure degrades to the plain gate."""
    if not enabled:
        return None
    try:
        from faster_whisper import vad  # noqa: F401 — availability probe
    except ImportError:
        return None
    lock = None
    if voice_lock:
        try:
            from .voicelock import VoiceLock, enrolled, ensure_model

            if enrolled():
                ensure_model()
                lock = VoiceLock()
                if not lock.load():
                    lock = None
        except Exception:
            lock = None
    return SileroSpeechCheck(voicelock=lock)


def _resample_to_16k(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    n_out = int(round(audio.shape[0] * 16000 / sample_rate))
    if n_out <= 0:
        return audio.astype(np.float32)
    x_old = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)

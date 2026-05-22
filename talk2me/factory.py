"""Provider construction, keyed off Config. The one place that maps config
strings to concrete classes.

Adding an engine (Silero VAD, Deepgram STT, ElevenLabs TTS) is a new branch here
plus a new class satisfying the Protocol — never an orchestrator or CLI change.
That containment is the whole point: no provider swap should ripple outward.
"""

from __future__ import annotations

from .config import Config
from .protocols import STT, TTS, VAD, AgentBackend


def frame_samples(cfg: Config) -> int:
    """Samples per VAD/mic frame. Mic and VAD MUST agree on this."""
    return int(cfg.sample_rate * cfg.frame_ms / 1000)


def build_vad(cfg: Config) -> VAD:
    if cfg.vad == "energy":
        from .vad import EnergyVAD

        return EnergyVAD(
            sample_rate=cfg.sample_rate,
            frame_samples=frame_samples(cfg),
            threshold=cfg.energy_threshold,
        )
    if cfg.vad == "silero":
        from .vad.silero import SileroVAD

        return SileroVAD(
            sample_rate=cfg.sample_rate,
            frame_samples=frame_samples(cfg),
            threshold=cfg.silero_threshold,
            model_path=cfg.silero_model_path,
        )
    if cfg.vad == "webrtc":
        from .vad.webrtc import WebrtcVAD

        return WebrtcVAD(
            sample_rate=cfg.sample_rate,
            frame_samples=frame_samples(cfg),
            aggressiveness=cfg.vad_aggressiveness,
        )
    raise ValueError(f"unknown vad: {cfg.vad!r}")


def build_stt(cfg: Config) -> STT:
    if cfg.stt == "whisper":
        from .stt import WhisperSTT

        return WhisperSTT(
            model=cfg.whisper_model,
            initial_prompt=_vocab_prompt(cfg.vocab),
        )
    raise ValueError(f"unknown stt: {cfg.stt!r}")


def build_tts(cfg: Config) -> TTS:
    if cfg.tts == "say":
        from .tts import SayTTS

        return SayTTS(voice=cfg.voice)
    if cfg.tts == "kitten":
        from .tts.kitten import KittenTTS

        return KittenTTS(voice=cfg.voice)
    if cfg.tts == "null":
        from .tts.null import NullTTS

        return NullTTS()
    raise ValueError(f"unknown tts: {cfg.tts!r}")


def build_backend(cfg: Config) -> AgentBackend:
    from .backends import ClaudeCodeBackend

    return ClaudeCodeBackend(
        claude_bin=cfg.claude_bin,
        model=cfg.model,
        cwd=cfg.cwd,
        permission_mode=cfg.permission_mode,
        extra_args=cfg.extra_claude_args,
    )


def _vocab_prompt(vocab: list[str]) -> str | None:
    """Bias terms whisper toward names it would otherwise mangle.

    Decoupled by design: talk2me knows nothing about any vault. A 2ndBrain user
    feeds aliases in via `--vocab-file`; everyone else passes their own terms.
    """
    if not vocab:
        return None
    return "Proper nouns and domain terms: " + ", ".join(vocab) + "."

"""Provider construction, keyed off Config. The one place that maps config
strings to concrete classes.

Adding an engine (Silero VAD, Deepgram STT, ElevenLabs TTS) is a new branch here
plus a new class satisfying the Protocol — never an orchestrator or CLI change.
That containment is the whole point: no provider swap should ripple outward.
"""

from __future__ import annotations

import os

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

        # Vocab rides as faster-whisper hotwords (whole-utterance biasing);
        # see the module docstring in stt/whisper.py for why not initial_prompt.
        return WhisperSTT(
            model=cfg.whisper_model,
            vocab=cfg.vocab,
            language=cfg.language,
        )
    if cfg.stt == "parakeet":
        from .stt.parakeet import ParakeetMLXSTT

        # No vocab: the TDT decoder has no hotword input (stt/parakeet.py).
        return ParakeetMLXSTT(model=cfg.parakeet_model)
    raise ValueError(f"unknown stt: {cfg.stt!r}")


def build_tts(cfg: Config) -> TTS:
    if cfg.tts == "say":
        from .tts import SayTTS

        return SayTTS(voice=cfg.voice, rate_wpm=cfg.rate_wpm)
    if cfg.tts == "kitten":
        from .tts.kitten import KittenTTS

        return KittenTTS(voice=cfg.voice)
    if cfg.tts == "null":
        from .tts.null import NullTTS

        return NullTTS()
    raise ValueError(f"unknown tts: {cfg.tts!r}")


def build_backend(cfg: Config) -> AgentBackend:
    from .backends import ClaudeCodeBackend

    from .config import VOICE_SYSTEM_PROMPT
    from .continuity import save_last_session as _save_session

    if cfg.agent == "codex":
        from .backends.codex import CodexBackend

        # Codex learns its thread id mid-turn (thread.started) — record it
        # then, so --continue and the spoken picker can find it later.
        return CodexBackend(
            model=cfg.model,
            cwd=cfg.cwd,
            resume_session_id=cfg.resume_session_id,
            extra_args=cfg.extra_claude_args,
            on_session=lambda sid: _save_session(cfg.cwd, sid),
        )

    # The stdio approval gate only makes sense when the CLI would otherwise
    # deny: bypass modes auto-approve everything, so wiring the prompt tool
    # there would never fire (and the denylist still applies CLI-side).
    stdio_gate = cfg.voice_approval and "bypass" not in cfg.permission_mode.lower()

    # Alternate brains (Kimi / GLM / DeepSeek / any Anthropic-compatible
    # endpoint) ride the same CLI via environment: base URL + the API key
    # read from the env var the user NAMED — never from a file.
    extra_env: dict[str, str] = {}
    if cfg.backend_base_url:
        extra_env["ANTHROPIC_BASE_URL"] = cfg.backend_base_url
        if cfg.backend_auth_env:
            key = os.environ.get(cfg.backend_auth_env, "")
            extra_env["ANTHROPIC_AUTH_TOKEN"] = key
            # Keep the CLI from preferring a logged-in Anthropic account.
            extra_env["ANTHROPIC_API_KEY"] = key

    from .continuity import save_last_session

    backend = ClaudeCodeBackend(
        claude_bin=cfg.claude_bin,
        model=cfg.model,
        cwd=cfg.cwd,
        permission_mode=cfg.permission_mode,
        extra_args=cfg.extra_claude_args,
        permission_prompt_stdio=stdio_gate,
        allowed_tools=cfg.allowed_tools,
        disallowed_tools=cfg.disallowed_tools,
        setting_sources=None if cfg.with_user_config else "project,local",
        append_system_prompt=(
            VOICE_SYSTEM_PROMPT if cfg.input_mode == "voice" else None
        ),
        extra_env=extra_env or None,
        resume_session_id=cfg.resume_session_id,
    )
    # Record this session for `--continue` next time, keyed by working dir.
    save_last_session(cfg.cwd, backend._session_id)
    return backend



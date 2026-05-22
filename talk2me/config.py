"""Runtime configuration. CLI flags map onto this; nothing else reads argv."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Config:
    # --- agent backend ---
    claude_bin: str = "claude"
    model: str | None = None
    cwd: str | None = None
    permission_mode: str = "default"

    # --- input ---
    input_mode: str = "voice"  # "voice" | "text"

    # --- audio ---
    sample_rate: int = 16000  # mic / STT rate
    frame_ms: int = 30  # VAD frame size

    # --- VAD / turn detection ---
    vad: str = "energy"  # "energy" | "silero"
    energy_threshold: float = 0.012  # RMS; tune per mic
    silence_ms: int = 900  # trailing silence that ends a turn
    min_speech_ms: int = 250  # ignore blips shorter than this

    # --- STT ---
    stt: str = "whisper"
    whisper_model: str = "base.en"
    vocab: list[str] = field(default_factory=list)  # bias terms (names, jargon)

    # --- TTS ---
    tts: str = "say"  # "say" | "kitten" | "null"
    voice: str | None = None  # engine-specific voice id

    # --- duplex / barge-in ---
    barge_in: bool = True  # full-duplex; requires headphones to avoid echo
    half_duplex: bool = False  # mute mic while speaking (no echo HW needed)

    # --- misc ---
    extra_claude_args: list[str] = field(default_factory=list)

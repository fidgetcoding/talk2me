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
    # Capture / playback device selection. Each is an index or a case-insensitive
    # name substring (e.g. "MacBook", "AirPods"); None = system default. Keeping
    # input and output independent is what lets the "BT headphones out + laptop
    # mic in" topology dodge the Bluetooth HFP trap (opening a BT mic forces the
    # whole headset into mono telephone-quality Hands-Free Profile).
    input_device: str | None = None
    output_device: str | None = None

    # --- VAD / turn detection ---
    vad: str = "energy"  # "energy" | "silero" | "webrtc"
    energy_threshold: float = 0.012  # RMS; tune per mic
    silero_threshold: float = 0.5  # speech-probability cutoff (0..1), silero only
    silero_model_path: str | None = None  # ONNX path; None → env/sibling default
    vad_aggressiveness: int = 2  # webrtc only: 0 (lenient) .. 3 (aggressive filtering)
    silence_ms: int = 900  # trailing silence that ends a turn
    min_speech_ms: int = 250  # ignore blips shorter than this
    # Hard ceiling on one utterance. A stuck-open VAD (noisy room, threshold set
    # below the noise floor) otherwise buffers audio forever: unbounded memory
    # AND a loop that never yields a turn. At the cap the segmenter force-emits
    # what it has, exactly as if silence had ended the turn. 0 disables.
    max_utterance_ms: int = 90_000

    # --- STT ---
    stt: str = "whisper"
    whisper_model: str = "base.en"
    vocab: list[str] = field(default_factory=list)  # bias terms (names, jargon)

    # --- TTS ---
    tts: str = "say"  # "say" | "kitten" | "null"
    voice: str | None = None  # engine-specific voice id

    # --- duplex / barge-in ---
    # Defaults match what the orchestrator actually does: half-duplex, mic muted
    # while the agent speaks (no echo-cancellation hardware needed). Full-duplex
    # barge-in (barge_in=True / half_duplex=False) is not implemented yet.
    barge_in: bool = False  # full-duplex; requires headphones to avoid echo
    half_duplex: bool = True  # mute mic while speaking (no echo HW needed)

    # --- misc ---
    extra_claude_args: list[str] = field(default_factory=list)
    debug: bool = False  # print VAD speech/turn transitions for threshold tuning

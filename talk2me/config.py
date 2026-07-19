"""Runtime configuration. CLI flags map onto this; nothing else reads argv."""

from __future__ import annotations

from dataclasses import dataclass, field

# Tools auto-approved with no voice gate: read-only + safe local inspection.
# Everything else (Edit, Write, other Bash, MCP tools) falls through to the
# spoken approve/deny gate. Rule syntax is the CLI's documented prefix form
# (`Bash(git status:*)`); both `:*` and ` *` verified working on 2.1.214 —
# see docs/permission-spike-results.md.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "Read",
    "Glob",
    "Grep",
    "TodoWrite",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(pwd)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git branch:*)",
    "Bash(rg:*)",
    "Bash(find:*)",
    "Bash(npm test:*)",
    "Bash(npm run:*)",
    "Bash(pytest:*)",
    "Bash(python -m pytest:*)",
)

# Hard-denied in every mode — never even asked out loud. Irreversible /
# exfiltration / privilege escalation. A voice user who isn't watching the
# screen should not be able to say "yes" to any of these.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(rm -rf:*)",
    "Bash(git push:*)",
    "Bash(git reset --hard:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(sudo:*)",
    "Bash(ssh:*)",
    "Bash(dd:*)",
)


# Appended to the agent's system prompt in voice mode. Shorter, spoken-shaped
# replies are also a latency win: fewer output tokens before the turn ends.
VOICE_SYSTEM_PROMPT = (
    "Your replies are spoken aloud by text-to-speech in a live voice "
    "conversation. Be brief and conversational: plain sentences only — no "
    "markdown, no headers, no bullet lists, no emoji. Never read code aloud; "
    "describe what you did or found in a sentence instead. When a request is "
    "ambiguous, make the reasonable assumption and state it in a short clause "
    "rather than asking multi-part clarifying questions."
)


@dataclass
class Config:
    # --- agent backend ---
    claude_bin: str = "claude"
    model: str | None = None
    cwd: str | None = None
    permission_mode: str = "default"
    # Load only project+local settings into the agent by default. The user's
    # global stack (hooks, skills, user CLAUDE.md) measurably hurts a voice
    # loop: +1.75s to first token on a trivial prompt, hook-driven Skill tool
    # calls before any speakable text, and stop-hook chatter ("nothing
    # noteworthy to save") spoken aloud. Project rules still load.
    with_user_config: bool = False
    # Spoken approve/deny gate for tool calls outside allowed/disallowed_tools.
    # Wires `--permission-prompt-tool stdio` so the CLI pauses the turn and asks
    # us instead of silently denying. Disabled automatically for bypass modes.
    voice_approval: bool = True
    allowed_tools: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS)
    )
    disallowed_tools: list[str] = field(
        default_factory=lambda: list(DEFAULT_DISALLOWED_TOOLS)
    )

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
    # Audio kept from just BEFORE speech onset and prepended to the utterance.
    # Energy VADs miss quiet first phonemes, and without this the transcript
    # starts mid-word (live-run: "count down to fifty" -> "down to 50").
    # 0 disables.
    pre_roll_ms: int = 300
    # Hard ceiling on one utterance. A stuck-open VAD (noisy room, threshold set
    # below the noise floor) otherwise buffers audio forever: unbounded memory
    # AND a loop that never yields a turn. At the cap the segmenter force-emits
    # what it has, exactly as if silence had ended the turn. 0 disables.
    max_utterance_ms: int = 90_000

    # --- STT ---
    stt: str = "whisper"  # "whisper" (CPU, hotword biasing) | "parakeet" (M-series GPU)
    whisper_model: str = "base.en"
    parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v2"
    vocab: list[str] = field(default_factory=list)  # bias terms (whisper only)

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

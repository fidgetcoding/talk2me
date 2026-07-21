"""CLI entrypoint. `python -m talk2me [flags]`.

Maps argv onto Config, builds the provider stack, runs the orchestrator. A
`--text` mode skips all audio (type instead of talk) for cheap loop testing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import factory
from .config import Config

# --- security caps / allowlists (security-audit.md M2, M3, H1, M1, L2) ---

# Permission modes safe to forward to `claude` verbatim. `bypassPermissions`
# (and any other auto-approve-everything mode) is deliberately excluded: under a
# voice loop, ambient audio → whisper → unconfirmed tool execution makes a silent
# bypass a full RCE-by-sound posture. See security-audit.md M2.
# ("manual" is the name newer CLI help shows for the classic prompt-me posture;
# "default" still parses and behaves identically — spike-verified on 2.1.214.)
SAFE_PERMISSION_MODES = ("default", "manual", "acceptEdits", "plan")

# Substrings that, if present in a permission-mode value, indicate an
# auto-approve-everything posture we never forward (defends gate #1 against a
# value reaching argv via the positional passthrough). security-audit.md M3.
_BYPASS_MODE_TOKENS = ("bypass", "skip")

# Permission-affecting flags rejected from the positional `claude_args` tail so
# they cannot smuggle a bypass posture past the --permission-mode allowlist.
# security-audit.md M3. The tool-rule flags are blocked because talk2me owns
# them (--allow-tool / --deny-tool) — a passthrough `--allowedTools Bash` would
# silently widen the auto-approve surface past the voice gate.
_BLOCKED_PASSTHROUGH_FLAGS = (
    "--dangerously-skip-permissions",
    "--add-dir",
    "--permission-prompt-tool",
    "--allowedTools",
    "--disallowedTools",
)

# --vocab-file resource ceilings. security-audit.md H1 / M1 / L2.
MAX_VOCAB_FILE_BYTES = 64 * 1024  # 64 KB on-disk cap; reject larger files.
MAX_VOCAB_TERMS = 500  # cap accumulated bias terms; stop once reached.


def _parse_args(argv: list[str]) -> Config:
    from .wizard import load_saved_config, run_wizard, should_run_first_time

    # Saved setup (~/.talk2me/config.json) becomes the DEFAULTS; explicit
    # flags always win. `--setup` re-runs the wizard; a first interactive
    # launch with no config and no identity flags runs it automatically.
    saved = load_saved_config()
    run_setup = "--setup" in argv or should_run_first_time(argv)
    argv = [x for x in argv if x != "--setup"]
    if run_setup and sys.stdin.isatty():
        saved = run_wizard(saved)

    p = argparse.ArgumentParser(prog="talk2me", description=__doc__)
    p.add_argument(
        "--setup", action="store_true",
        help="run the guided setup (brain, ears, voice, barge-in, tools, "
        "folder), save it as your defaults, then launch",
    )
    p.add_argument(
        "--agent", default="claude", choices=["claude", "codex"],
        help=(
            "which coding agent is the brain. claude = Claude Code CLI "
            "(default). codex = OpenAI's Codex CLI (`codex login` first; "
            "tool safety comes from its sandbox, so --gated doesn't apply)."
        ),
    )
    p.add_argument("--model", default=None, help="claude model (e.g. haiku, sonnet)")
    p.add_argument(
        "--backend-base-url", default=None, dest="backend_base_url",
        help=(
            "Anthropic-compatible endpoint for a different brain — Kimi "
            "(https://api.moonshot.ai/anthropic), GLM, DeepSeek all publish "
            "one. Pair with --backend-auth-env."
        ),
    )
    p.add_argument(
        "--backend-auth-env", default=None, dest="backend_auth_env",
        help=(
            "NAME of the env var holding the API key for --backend-base-url "
            "(e.g. MOONSHOT_API_KEY). The key itself never lands in a file."
        ),
    )
    p.add_argument("--cwd", default=None, help="working dir for the agent")
    p.add_argument(
        "--gated", action="store_true",
        help=(
            "spoken-approval mode: tools outside a read-only allowlist pause "
            "the turn and ask out loud ('approve or deny?'). The DEFAULT is "
            "auto-approve: tools just run, like the Claude app — except the "
            "hard denylist (sudo, rm -rf, git push, curl…), which stays "
            "blocked in every mode."
        ),
    )
    p.add_argument(
        "--permission-mode", default="default", choices=SAFE_PERMISSION_MODES,
        help="fine-tune the posture used WITH --gated (bypass values blocked here)",
    )
    p.add_argument("--text", action="store_true", help="type instead of talk (no audio)")
    p.add_argument(
        "--dangerously-allow-tools", action="store_true",
        help=(
            "TEXT MODE ONLY. Forward --permission-mode bypassPermissions to "
            "`claude`, auto-approving every tool call (Bash, Write, git push, MCP "
            "mutations) with NO confirmation. Refused in voice mode: ambient audio "
            "→ whisper → silent tool execution is a full RCE-by-sound posture. Use "
            "only when you fully trust the typed input."
        ),
    )
    p.add_argument(
        "--no-voice-approval", action="store_true",
        help=(
            "disable the spoken approve/deny gate for tool calls. Tools outside "
            "the allowlist are then silently denied by the CLI (safe, but the "
            "agent can't do one-off gated actions)."
        ),
    )
    p.add_argument(
        "--allow-tool", action="append", default=[],
        help=(
            "extra auto-approved tool rule, repeatable — e.g. "
            "--allow-tool 'Bash(make:*)'. Added to the built-in read-only "
            "allowlist; everything else goes through the voice gate."
        ),
    )
    p.add_argument(
        "--deny-tool", action="append", default=[],
        help=(
            "extra hard-denied tool rule, repeatable — denied in every mode, "
            "never asked out loud."
        ),
    )
    p.add_argument(
        "--stt", default="whisper", choices=["whisper", "parakeet"],
        help=(
            "transcription engine. whisper (default): CPU, supports --vocab "
            "hotword biasing. parakeet: Apple-Silicon GPU via MLX — more "
            "accurate AND ~10x faster, ~2 GB RAM, English-only, no vocab "
            "biasing (`pip install -e \".[parakeet]\"`)."
        ),
    )
    p.add_argument("--whisper-model", default="base.en")
    p.add_argument(
        "--language", default="en",
        help=(
            "transcription language (whisper only): a 2-letter code (es, de, "
            "fr…) or 'auto' to detect per utterance. Non-English needs a "
            "multilingual model, e.g. --whisper-model base — the .en models "
            "are English-only, and so is parakeet. Voice COMMANDS (pause/"
            "wake/approve) stay English for now."
        ),
    )
    p.add_argument(
        "--tts", default="say", choices=["say", "kitten", "null"],
        help="speech engine: say (macOS built-in), kitten (local neural, "
        "`pip install talk2me[kitten]`), null (no voice out)",
    )
    p.add_argument("--voice", default=None, help="engine-specific voice id")
    p.add_argument(
        "--rate", type=int, default=236, dest="rate_wpm",
        help="speech rate in words/minute (say engine; macOS default ~175, "
        "talk2me default 236 ≈ 1.35x).",
    )
    p.add_argument(
        "--vad", default="energy", choices=["energy", "silero", "webrtc"],
        help="voice-activity detector. webrtc is more robust across mics (BT).",
    )
    p.add_argument(
        "--vad-aggressiveness", type=int, default=2, choices=[0, 1, 2, 3],
        help="webrtc only: 0 (lenient) .. 3 (aggressive noise filtering)",
    )
    p.add_argument("--energy-threshold", type=float, default=0.012)
    p.add_argument(
        "--silence-ms", type=int, default=1200,
        help="trailing silence (ms) that ends your turn; lower = snappier but "
        "cuts off mid-sentence thinking pauses",
    )
    p.add_argument(
        "--list-devices", action="store_true",
        help="print available input/output audio devices and exit",
    )
    p.add_argument(
        "--input-device", default=None,
        help="mic device: PortAudio index or name substring (e.g. MacBook)",
    )
    p.add_argument(
        "--output-device", default=None,
        help="playback device: index or name substring (e.g. AirPods)",
    )
    p.add_argument(
        "--vocab", action="append", default=[],
        help="bias term for STT (repeatable): --vocab Lorecraft --vocab Morgen",
    )
    p.add_argument(
        "--vocab-file", default=None,
        help="file of bias terms, one per line or comma-separated",
    )
    p.add_argument(
        "--with-user-config", action="store_true",
        help=(
            "load your full user-level Claude config (hooks, skills, user "
            "CLAUDE.md) into the agent. Off by default: it measurably slows "
            "every turn and its hook chatter gets spoken aloud. Project-level "
            "CLAUDE.md / settings always load either way."
        ),
    )
    p.add_argument(
        "--barge-in", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "full-duplex (ON by default): the mic stays live while the agent "
            "speaks; start talking and playback stops, the agent's turn is "
            "interrupted. Works on headphones AND open-air speakers — on "
            "speakers the echo gate filters its own voice out of the mic, so "
            "only sound it isn't making can cut it. --no-barge-in forces "
            "half-duplex."
        ),
    )
    p.add_argument(
        "--aec", choices=["auto", "native", "gate", "off"], default="auto",
        help=(
            "how speakers barge-in filters the agent's own voice. auto "
            "(default): macOS driver-level echo cancellation when it probes "
            "healthy, else the userspace echo gate. native/gate force a "
            "layer; off restores the old behavior (speakers mute the ears "
            "while it speaks)."
        ),
    )
    p.add_argument(
        "--no-aec", dest="aec", action="store_const", const="off",
        help="alias for --aec off (kept from v2.4)",
    )
    p.add_argument(
        "--save-dir", default=os.environ.get("TALK2ME_SAVE_DIR") or None,
        help="save a plain-markdown transcript of every session into this "
        "folder (persistent default: export TALK2ME_SAVE_DIR=~/talk2me-logs)",
    )
    p.add_argument(
        "--ticks", action=argparse.BooleanOptionalAction, default=False,
        help=(
            "audible 'still working' blip every ~8 quiet seconds during long "
            "tool runs. OFF by default — the work panel shows it on screen."
        ),
    )
    p.add_argument(
        "--continue", "-c", action="store_true", dest="resume",
        help=(
            "pick up where you left off: resume the last session for this "
            "working directory (the agent keeps its memory of what you built)"
        ),
    )
    p.add_argument(
        "--phone", action="store_true",
        help=(
            "use your phone as the mic + speaker (for SSH sessions from an "
            "iPhone/iPad). Serves a one-tap page on localhost; forward the "
            "port in your SSH app (Blink: -L 8765:localhost:8765) and open "
            "http://localhost:8765 in Safari. Same loop, same barge-in — the "
            "phone's own echo cancellation does the heavy lifting."
        ),
    )
    p.add_argument(
        "--phone-port", type=int, default=8765,
        help="port for the --phone bridge (default 8765)",
    )
    p.add_argument(
        "--voice-lock", action=argparse.BooleanOptionalAction, default=False,
        dest="voice_lock",
        help=(
            "solo mode: the mic answers ONLY to your enrolled voice (other "
            "people, the TV, its own speaker output are ignored). Needs a "
            "one-time --enroll-voice. Flip live by saying 'team session' / "
            "'solo session'."
        ),
    )
    p.add_argument(
        "--enroll-voice", action="store_true", dest="enroll_voice",
        help=(
            "record your voiceprint (read 3 sentences, ~20s), calibrate the "
            "lock against the agent's own voice, then launch with voice-lock "
            "on. Re-run any time to re-calibrate."
        ),
    )
    p.add_argument(
        "--speech-check", action=argparse.BooleanOptionalAction, default=True,
        help=(
            "confirm audio is actually speech (Silero classifier) before "
            "interrupting the agent or transcribing — rejects typing, taps, "
            "coughs. --no-speech-check restores raw-VAD behavior."
        ),
    )
    p.add_argument(
        "--plain", action="store_true",
        help=(
            "classic v1 output: no colors, no panels. Auto-selected when "
            "stdout isn't a terminal, NO_COLOR is set, or rich is missing."
        ),
    )
    p.add_argument(
        "--debug", action="store_true",
        help="print VAD speech/turn transitions (for tuning --energy-threshold)",
    )
    p.add_argument("claude_args", nargs="*", help="extra args passed to `claude`")
    if saved:
        p.set_defaults(**{k: v for k, v in saved.items() if v is not None})
    a = p.parse_args(argv)

    # --list-devices is a pure query: print the device table and exit before any
    # provider/agent is built. Kept here (not in main) so it shares one parser.
    if a.list_devices:
        from .audio import format_device_table

        print(format_device_table())
        raise SystemExit(0)

    input_mode = "text" if a.text else "voice"

    # Default posture: auto-approve (bypassPermissions) with the scoped
    # denylist still enforced — Anthropic's deny rules apply in EVERY mode,
    # bypass included, so the catastrophic verbs stay blocked while everything
    # else flows without spoken gates. --gated restores the approval loop.
    if a.gated:
        permission_mode = a.permission_mode
        voice_approval = not a.no_voice_approval
    else:
        permission_mode = "bypassPermissions"
        voice_approval = False

    # --dangerously-allow-tools remains the ONLY way to drop the denylist too,
    # and it still requires --text: ambient audio never gets a zero-guardrail
    # posture. (security-audit.md M2)
    dangerously = False
    if a.dangerously_allow_tools:
        if input_mode != "text":
            p.error(
                "--dangerously-allow-tools requires --text mode; refusing a "
                "zero-guardrail posture on ambient-audio (voice) input"
            )
        permission_mode = "bypassPermissions"
        voice_approval = False
        dangerously = True

    # M3: reject permission-affecting flags smuggled through the positional tail.
    _reject_blocked_passthrough(p, a.claude_args)

    # A named key variable that isn't exported fails HERE with a sentence,
    # not twenty turns later with an opaque auth error from the CLI.
    if a.backend_base_url and a.backend_auth_env and not os.environ.get(a.backend_auth_env):
        p.error(
            f"--backend-auth-env names {a.backend_auth_env!r} but it isn't "
            f"set — run: export {a.backend_auth_env}=<your api key>"
        )

    from .config import DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS

    return Config(
        debug=a.debug,
        save_dir=a.save_dir,
        working_ticks=a.ticks,
        plain=a.plain,
        speech_check=a.speech_check,
        voice_lock=a.voice_lock or a.enroll_voice,
        enroll_voice=a.enroll_voice,
        phone=a.phone,
        phone_port=a.phone_port,
        agent=a.agent,
        model=a.model,
        cwd=a.cwd,
        resume_session_id=_resolve_resume(a),
        backend_base_url=a.backend_base_url,
        backend_auth_env=a.backend_auth_env,
        permission_mode=permission_mode,
        with_user_config=a.with_user_config,
        voice_approval=voice_approval,
        allowed_tools=list(DEFAULT_ALLOWED_TOOLS) + a.allow_tool,
        disallowed_tools=(
            [] if dangerously else list(DEFAULT_DISALLOWED_TOOLS) + a.deny_tool
        ),
        barge_in=a.barge_in,
        half_duplex=not a.barge_in,
        aec=a.aec,
        input_mode=input_mode,
        stt=a.stt,
        whisper_model=a.whisper_model,
        language=a.language,
        tts=a.tts,
        voice=a.voice,
        rate_wpm=a.rate_wpm,
        vad=a.vad,
        vad_aggressiveness=a.vad_aggressiveness,
        energy_threshold=a.energy_threshold,
        silence_ms=a.silence_ms,
        input_device=a.input_device,
        output_device=a.output_device,
        vocab=_collect_vocab(p, a.vocab, a.vocab_file),
        extra_claude_args=a.claude_args,
    )


def _resolve_resume(a) -> str | None:
    """--continue -> the last session id recorded for this working dir."""
    if not a.resume:
        return None
    from .continuity import load_last_session

    sid = load_last_session(a.cwd)
    if sid is None:
        print(
            "(no previous session for this folder — starting fresh)",
            flush=True,
        )
    return sid


def _reject_blocked_passthrough(
    p: argparse.ArgumentParser, claude_args: list[str]
) -> None:
    """Fail cleanly if the passthrough tail carries a permission-bypass flag.

    Guards against `--dangerously-skip-permissions`, `--add-dir`, or
    `--permission-mode <bypass>` slipping past the --permission-mode allowlist
    via the positional escape hatch. security-audit.md M3.
    """
    for i, raw in enumerate(claude_args):
        # Split `--flag=value` so the flag name is matched independent of value.
        flag = raw.split("=", 1)[0]
        if flag in _BLOCKED_PASSTHROUGH_FLAGS:
            p.error(f"refusing dangerous passthrough flag: {flag}")
        if flag == "--permission-mode":
            # value may be inline (--permission-mode=X) or the next token.
            value = raw.split("=", 1)[1] if "=" in raw else (
                claude_args[i + 1] if i + 1 < len(claude_args) else ""
            )
            if any(tok in value.lower() for tok in _BYPASS_MODE_TOKENS):
                p.error(
                    f"refusing --permission-mode bypass value in passthrough: "
                    f"{value!r}"
                )


def _collect_vocab(
    p: argparse.ArgumentParser, terms: list[str], path: str | None
) -> list[str]:
    """Load bias terms from CLI flags + an optional --vocab-file.

    The file is validated and bounded: it must be an existing regular file (no
    dirs, symlinks, devices, or FIFOs), under MAX_VOCAB_FILE_BYTES, and yields at
    most MAX_VOCAB_TERMS terms. Any path/IO error is reported as a clean argparse
    error, never a traceback. security-audit.md H1 / M1 / L2.
    """
    out = list(terms)
    if not path:
        return out

    # Expand ~ but do NOT realpath() before the lstat — realpath would follow a
    # symlink and the S_ISLNK guard below would never fire (M1: reject symlinks).
    expanded = os.path.expanduser(path)
    try:
        st = os.lstat(expanded)
    except (FileNotFoundError, OSError) as exc:
        p.error(f"--vocab-file not accessible: {path} ({exc.strerror or exc})")

    # Reject symlinks, directories, devices, FIFOs — anything not a plain file.
    import stat as _stat

    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
        p.error(f"--vocab-file must be a regular file (not a symlink/dir/device): {path}")
    if st.st_size > MAX_VOCAB_FILE_BYTES:
        p.error(
            f"--vocab-file too large: {st.st_size} bytes "
            f"(max {MAX_VOCAB_FILE_BYTES})"
        )

    try:
        # O_NOFOLLOW: refuse to open through a symlink even if it appeared between
        # the lstat above and this open (TOCTOU hardening).
        fd = os.open(expanded, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fh:
            # Bounded read: never pull more than the size cap into memory even if
            # the file grew or has no newlines (a single unbroken "line").
            data = fh.read(MAX_VOCAB_FILE_BYTES + 1)
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        p.error(f"--vocab-file could not be read: {path} ({exc})")

    for line in data.splitlines():
        for term in line.split(","):
            term = term.strip()
            if term:
                out.append(term)
                if len(out) >= MAX_VOCAB_TERMS:
                    return out
    return out


async def _run_voice(cfg: Config) -> tuple[int, bool]:
    """Run one voice session. Returns (exit_code, edit_requested) — the
    second is True when Ctrl-T (macOS SIGINFO) asked for the settings
    wizard: main() tears us down, re-runs setup, and relaunches."""
    import contextlib
    import signal

    from .audio import Mic, Speaker, output_is_speakers, resolve_device
    from .orchestrator import Orchestrator
    from .render import build_renderer

    renderer = build_renderer(cfg)

    # Voice-lock enrollment: explicit (--enroll-voice) or automatic when the
    # lock is wanted but no voiceprint exists yet. Runs BEFORE the voice
    # stack so the calibration mic has the room to itself.
    if cfg.voice_lock and sys.stdin.isatty():
        from .voicelock import enrolled, run_enrollment

        if cfg.enroll_voice or not enrolled():
            if await run_enrollment(cfg):
                # Persist the lock as the default — enrolling and then
                # launching unlocked surprised in the field (a video's voice
                # walked straight in because plain t2m never armed the lock).
                from .wizard import load_saved_config, save_config

                saved = load_saved_config()
                saved["voice_lock"] = True
                save_config(saved)
                renderer.status_note(
                    "voice-lock is now your DEFAULT for every launch "
                    "(--no-voice-lock or the wizard turns it off)"
                )
            else:
                renderer.status_note(
                    "enrollment didn't finish — voice-lock OFF this session"
                )
                cfg.voice_lock = False
    elif cfg.voice_lock:
        from .voicelock import enrolled

        if not enrolled():
            renderer.status_note(
                "voice-lock needs enrollment (t2m --enroll-voice) — OFF"
            )
            cfg.voice_lock = False

    # The speech gate is built EARLY so the speaker-downgrade decision can
    # see whether a healthy voice-lock enables echo-guarded talk-over.
    from .speechcheck import build_speech_check

    speech_check = build_speech_check(cfg.speech_check, voice_lock=cfg.voice_lock)
    if cfg.voice_lock and (
        speech_check is None or getattr(speech_check, "voicelock", None) is None
    ):
        renderer.status_note(
            "voice-lock couldn't load its model — running unlocked"
        )
        cfg.voice_lock = False
    lock_healthy = (
        cfg.voice_lock
        and speech_check is not None
        and getattr(speech_check, "voicelock", None) is not None
        and not getattr(speech_check.voicelock, "meta", {}).get("degraded", True)
    )
    if cfg.voice_lock and not lock_healthy and getattr(
        speech_check, "voicelock", None
    ) is not None:
        renderer.status_note(
            "voice-lock: calibration is weak on this mic/room — OBSERVING "
            "only (nothing gets blocked; --debug shows the scores). A "
            "quiet-room re-enroll can upgrade it to enforcing."
        )
        cfg.voice_lock_observing = True

    tts = factory.build_tts(cfg)
    bridge = None
    # Playback reference for the echo gate; stays None off-speakers, in
    # phone mode (the phone's own AEC covers it), and under --no-aec.
    echo_ref = None

    if cfg.phone:
        # Phone mode: the mic and speaker live on the phone, reached through
        # the SSH session's own port-forward. No local devices are touched,
        # and the speaker-downgrade check doesn't apply — the phone's echo
        # cancellation is what makes barge-in workable on its speaker.
        from .phone import PhoneBridge, WebMic, WebSpeaker

        bridge = PhoneBridge(cfg.phone_port)
        await bridge.start()
        renderer.status_note(
            f"phone bridge on {bridge.url()} — forward the port in your SSH "
            f"app (Blink: -L {cfg.phone_port}:localhost:{cfg.phone_port}), "
            "then open that URL in the phone's Safari and tap connect"
        )
        renderer.status_note("waiting for the phone…")
        await bridge.wait_connected()
        renderer.status_note("phone connected 📱")
        mic = WebMic(bridge, factory.frame_samples(cfg))
        speaker = WebSpeaker(bridge, tts.sample_rate)
    else:
        # Resolve name/index specs to PortAudio indices up front so a typo'd
        # device fails with a clean message before the agent process spins up.
        # Independent input/output indices are what enable the "BT out +
        # laptop mic in" topology.
        try:
            input_idx = resolve_device(cfg.input_device, "input")
            output_idx = resolve_device(cfg.output_device, "output")
        except ValueError as exc:
            renderer.device_error(str(exc))
            return 2, False

        # Adaptive duplex: --barge-in on open-air speakers (system default
        # included) used to downgrade to half-duplex — the mic would hear the
        # TTS and cut every answer on its own echo. The echo gate makes full
        # duplex safe instead: the Speaker records exactly what it plays, and
        # any mic sound the playback can't explain is by definition not its
        # own voice. (This superseded the voice-lock echo-guard, which needed
        # a speaker-ID separation real laptop mics don't deliver.)
        native_aec = False
        if cfg.barge_in and output_is_speakers(output_idx):
            try_native = cfg.aec in ("auto", "native")
            if try_native and cfg.input_device is not None:
                # The OS voice processor follows the system default mic — an
                # explicit --input-device must stay honored, so the gate
                # covers this topology.
                if cfg.aec == "native":
                    renderer.status_note(
                        "--aec native can't honor --input-device (the OS "
                        "voice processor follows the system default mic) — "
                        "using the echo gate instead"
                    )
                try_native = False
            if try_native:
                from . import vpio

                # probe() also warms the engine the session will reuse; a
                # wedged/unavailable voice processor fails here, BEFORE the
                # session starts with a silent mic.
                native_aec = vpio.probe()
                if not native_aec and cfg.aec == "native":
                    renderer.status_note(
                        "--aec native didn't probe healthy on this machine — "
                        "using the echo gate instead"
                    )
            if native_aec:
                cfg.aec_active = True
                cfg.aec_layer = "native"
                cfg.half_duplex = False
                renderer.status_note(
                    "speakers + barge-in: macOS voice processing subtracts "
                    "its own audio from the mic at the driver — talk over it "
                    "any time, any volume (--aec gate|off for the old layers)"
                )
                if cfg.vad == "webrtc":
                    # Measured 2026-07-20: webrtc calls the voice processor's
                    # noise-suppression hiss "voiced" on 60-76% of silent
                    # frames (any aggressiveness), so turns close seconds
                    # late or hold for minutes. The NS-flattened floor is
                    # exactly what the energy threshold wants: 0% idle,
                    # 2-3% during/after playback.
                    cfg.vad = "energy"
                    renderer.status_note(
                        "native AEC: webrtc VAD misreads the processed "
                        "signal (holds turns open) — using the energy VAD "
                        "here"
                    )
            elif cfg.aec != "off":
                from .echogate import EchoRef

                echo_ref = EchoRef(tts.sample_rate)
                cfg.aec_active = True
                cfg.aec_layer = "gate"
                cfg.half_duplex = False
                renderer.status_note(
                    "speakers + barge-in: its own voice off the speakers is "
                    "filtered out — talk over it any time (--no-aec restores "
                    "the old mute-while-speaking behavior)"
                )
            else:
                renderer.speaker_downgrade()
                cfg.barge_in = False
                cfg.half_duplex = True
                cfg.barge_downgraded = True

        if native_aec:
            from .vpio import VoiceProcessingMic

            mic = VoiceProcessingMic(cfg.sample_rate, factory.frame_samples(cfg))
        else:
            mic = Mic(
                cfg.sample_rate, factory.frame_samples(cfg), device=input_idx
            )
        speaker = Speaker(tts.sample_rate, device=output_idx, echo_ref=echo_ref)

    session_log = None
    if cfg.save_dir:
        from .sessionlog import SessionLog

        # An unwritable transcript dir must never kill the launch (live bug:
        # a typo'd absolute path at / crashed on the read-only filesystem).
        try:
            session_log = SessionLog(
                cfg.save_dir, model=cfg.model, stt=cfg.stt, cwd=cfg.cwd
            )
        except OSError as exc:
            renderer.error(
                f"[save-dir] can't write {cfg.save_dir} ({exc}) — "
                "transcripts OFF this session"
            )
        else:
            renderer.transcript_path(session_log.path)

    echo_gate = None
    if echo_ref is not None:
        from .echogate import EchoGate

        echo_gate = EchoGate(echo_ref)

    orch = Orchestrator(
        cfg=cfg,
        backend=factory.build_backend(cfg),
        vad=factory.build_vad(cfg),
        stt=factory.build_stt(cfg),
        tts=tts,
        mic=mic,
        speaker=speaker,
        session_log=session_log,
        renderer=renderer,
        speech_check=speech_check,
        echo_gate=echo_gate,
    )

    # Ctrl-T mid-session (macOS sends SIGINFO) = "open the settings menu":
    # tear the loop down cleanly, run the wizard, relaunch with new settings.
    edit_ev = asyncio.Event()
    loop = asyncio.get_running_loop()
    siginfo = getattr(signal, "SIGINFO", None)
    if siginfo is not None and sys.stdin.isatty():
        try:
            loop.add_signal_handler(siginfo, edit_ev.set)
        except (NotImplementedError, RuntimeError):
            siginfo = None

    run_task = asyncio.create_task(orch.run())
    edit_task = asyncio.create_task(edit_ev.wait())
    try:
        try:
            done, _pending = await asyncio.wait(
                {run_task, edit_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl-C: retrieve the session task before the loop tears down —
            # otherwise asyncio spews 'Task exception was never retrieved' +
            # 'Task was destroyed but it is pending' over the goodbye
            # (live-observed). Best-effort, then let main() print 'bye.'
            edit_task.cancel()
            run_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await run_task
            raise
        if run_task in done:
            edit_task.cancel()
            await run_task  # surface any exception from the session
            return 0, False
        # Ctrl-T: cancel the session; run()'s finally starts the teardown but
        # cancellation can clip its awaits — re-close everything (all no-op-
        # safe) so the wizard gets a quiet terminal and a dead mic.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        with contextlib.suppress(Exception):
            orch.mic.stop()
            orch.speaker.stop()
        with contextlib.suppress(Exception):
            await orch.backend.close()
        with contextlib.suppress(Exception):
            orch.render.close()
        return 0, True
    finally:
        if siginfo is not None:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(siginfo)
        if bridge is not None:
            with contextlib.suppress(Exception):
                await bridge.close()


async def _run_text(cfg: Config) -> int:
    """Audio-free loop: read stdin lines, print agent text, no TTS/STT."""
    import json

    from .events import (
        AssistantTextDelta,
        BackendError,
        PermissionRequest,
        ToolActivity,
        TurnComplete,
    )

    session_log = None
    if cfg.save_dir:
        from .sessionlog import SessionLog

        try:
            session_log = SessionLog(
                cfg.save_dir, model=cfg.model, stt="(text mode)", cwd=cfg.cwd
            )
        except OSError as exc:
            print(
                f"[save-dir] can't write {cfg.save_dir} ({exc}) — "
                "transcripts OFF this session",
                flush=True,
            )
        else:
            print(f"📝 saving transcript to {session_log.path}", flush=True)

    backend = factory.build_backend(cfg)
    await backend.start()
    events = backend.events()
    print(
        "talk2me (text mode) — type a message, Ctrl-D to quit. "
        "Created by @fidgetcoding :)\n",
        flush=True,
    )
    # Mirrors Orchestrator._fatal: a BackendError means the process is gone, so
    # stop the REPL outright instead of parking on readline against a corpse
    # (the user would otherwise discover the death only after typing a line).
    fatal = False
    try:
        while not fatal:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if session_log:
                session_log.user(line)
            try:
                await backend.send(line)
            except (BrokenPipeError, ConnectionError, RuntimeError, OSError) as exc:
                print(f"\n[error] backend unavailable: {exc}", flush=True)
                break
            # Same dedupe as the voice loop: the stream path announces a tool
            # by name, the full-message twin (upgrade) carries the detail.
            announced: list[str] = []
            async for ev in events:
                if isinstance(ev, AssistantTextDelta):
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
                elif isinstance(ev, ToolActivity):
                    if ev.upgrade and ev.name in announced:
                        announced.remove(ev.name)
                        if ev.summary:
                            print(f"   ↳ {ev.summary}", flush=True)
                        for line in ev.body.splitlines():
                            print(f"   │ {line}", flush=True)
                        if session_log:
                            session_log.tool(ev.name, ev.summary)
                    else:
                        detail = f" — {ev.summary}" if ev.summary else ""
                        print(f"\n[tool] {ev.name}{detail}", flush=True)
                        for line in ev.body.splitlines():
                            print(f"   │ {line}", flush=True)
                        if ev.upgrade:
                            if session_log:
                                session_log.tool(ev.name, ev.summary)
                        else:
                            announced.append(ev.name)
                elif isinstance(ev, PermissionRequest):
                    # Typed twin of the spoken gate: the CLI is paused on this
                    # request, so read one line and answer. EOF/empty -> deny.
                    print(
                        f"\n[permission] {ev.tool_name}: "
                        f"{json.dumps(ev.tool_input)[:300]}",
                        flush=True,
                    )
                    print("approve? [y/N] ", end="", flush=True)
                    ans = (await asyncio.to_thread(sys.stdin.readline)) or ""
                    allow = ans.strip().lower() in ("y", "yes", "approve", "allow")
                    await backend.respond_permission(ev.request_id, allow)
                elif isinstance(ev, TurnComplete):
                    if session_log:
                        session_log.assistant(ev.text)
                    print("\n", flush=True)
                    break
                elif isinstance(ev, BackendError):
                    print(f"\n[error] {ev.message}", flush=True)
                    fatal = True
                    break
    finally:
        await backend.close()
    return 1 if fatal else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        while True:
            cfg = _parse_args(args)
            if cfg.input_mode == "text":
                return asyncio.run(_run_text(cfg))
            code, edit_requested = asyncio.run(_run_voice(cfg))
            if not edit_requested:
                return code
            # Ctrl-T: session is down — run the wizard, then loop. The next
            # _parse_args re-reads the saved file as defaults (explicit
            # flags on the original command line still win).
            from .wizard import load_saved_config, run_wizard

            run_wizard(load_saved_config())
    except KeyboardInterrupt:
        print("\nbye.", flush=True)
        # Hard exit, deliberately: interpreter teardown after Ctrl-C kept
        # crashing — non-daemon executor threads stuck in a CoreAudio write
        # hung the exit, and daemon teardown threads inside Pa_StopStream
        # raced sounddevice's atexit Pa_Terminate into a segfault (both
        # live-observed 2026-07-19/20). Everything that matters is already
        # on disk (the transcript flushes per write); the OS reclaims audio
        # handles more reliably than a dying interpreter does.
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())

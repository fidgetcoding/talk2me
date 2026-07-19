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
    p = argparse.ArgumentParser(prog="talk2me", description=__doc__)
    p.add_argument("--model", default=None, help="claude model (e.g. haiku, sonnet)")
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
        "--barge-in", action="store_true",
        help=(
            "full-duplex: keep the mic live while the agent speaks; when you "
            "start talking, playback stops and the agent's turn is interrupted. "
            "REQUIRES HEADPHONES — with speakers the mic hears the TTS and "
            "self-triggers (no echo cancellation)."
        ),
    )
    p.add_argument(
        "--save-dir", default=os.environ.get("TALK2ME_SAVE_DIR") or None,
        help="save a plain-markdown transcript of every session into this "
        "folder (persistent default: export TALK2ME_SAVE_DIR=~/talk2me-logs)",
    )
    p.add_argument(
        "--no-ticks", action="store_true",
        help="disable the soft 'still working' blip during long tool runs",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="print VAD speech/turn transitions (for tuning --energy-threshold)",
    )
    p.add_argument("claude_args", nargs="*", help="extra args passed to `claude`")
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

    from .config import DEFAULT_ALLOWED_TOOLS, DEFAULT_DISALLOWED_TOOLS

    return Config(
        debug=a.debug,
        save_dir=a.save_dir,
        working_ticks=not a.no_ticks,
        model=a.model,
        cwd=a.cwd,
        permission_mode=permission_mode,
        with_user_config=a.with_user_config,
        voice_approval=voice_approval,
        allowed_tools=list(DEFAULT_ALLOWED_TOOLS) + a.allow_tool,
        disallowed_tools=(
            [] if dangerously else list(DEFAULT_DISALLOWED_TOOLS) + a.deny_tool
        ),
        barge_in=a.barge_in,
        half_duplex=not a.barge_in,
        input_mode=input_mode,
        stt=a.stt,
        whisper_model=a.whisper_model,
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


async def _run_voice(cfg: Config) -> int:
    from .audio import Mic, Speaker, output_is_speakers, resolve_device
    from .orchestrator import Orchestrator

    # Resolve name/index specs to PortAudio indices up front so a typo'd device
    # fails with a clean message before the agent process spins up. Independent
    # input/output indices are what enable the "BT out + laptop mic in" topology.
    try:
        input_idx = resolve_device(cfg.input_device, "input")
        output_idx = resolve_device(cfg.output_device, "output")
    except ValueError as exc:
        print(f"[device] {exc}\n\nRun `talk2me --list-devices` to see options.", flush=True)
        return 2

    # Adaptive duplex: --barge-in is the user's intent, but if the audio is
    # about to come out of open-air speakers (system default included), the mic
    # would hear the TTS and cut every answer on its own echo. Downgrade to the
    # safe half-duplex loop for this session instead of misbehaving.
    if cfg.barge_in and output_is_speakers(output_idx):
        print(
            "🔈 speakers on the output — barge-in off for this session so I "
            "don't argue with my own echo. Plug in headphones to interrupt me.",
            flush=True,
        )
        cfg.barge_in = False
        cfg.half_duplex = True

    session_log = None
    if cfg.save_dir:
        from .sessionlog import SessionLog

        session_log = SessionLog(
            cfg.save_dir, model=cfg.model, stt=cfg.stt, cwd=cfg.cwd
        )
        print(f"📝 saving transcript to {session_log.path}", flush=True)

    tts = factory.build_tts(cfg)
    orch = Orchestrator(
        cfg=cfg,
        backend=factory.build_backend(cfg),
        vad=factory.build_vad(cfg),
        stt=factory.build_stt(cfg),
        tts=tts,
        mic=Mic(cfg.sample_rate, factory.frame_samples(cfg), device=input_idx),
        speaker=Speaker(tts.sample_rate, device=output_idx),
        session_log=session_log,
    )
    await orch.run()
    return 0


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

        session_log = SessionLog(
            cfg.save_dir, model=cfg.model, stt="(text mode)", cwd=cfg.cwd
        )
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
            async for ev in events:
                if isinstance(ev, AssistantTextDelta):
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
                elif isinstance(ev, ToolActivity):
                    print(f"\n[tool] {ev.name}", flush=True)
                    if session_log:
                        session_log.tool(ev.name)
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
    cfg = _parse_args(argv if argv is not None else sys.argv[1:])
    runner = _run_text if cfg.input_mode == "text" else _run_voice
    try:
        return asyncio.run(runner(cfg))
    except KeyboardInterrupt:
        print("\nbye.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

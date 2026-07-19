"""First-run setup — pick the brain, ears, voice, and manners once, keep them.

Runs automatically on a first interactive voice launch (no saved config, no
identity flags on the command line), on demand via `t2m --setup`, and mid-
session via Ctrl-T (macOS). Answers land in ~/.talk2me/config.json and become
the DEFAULTS for every launch; explicit CLI flags always win over the file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from rich.console import Console
from rich.prompt import Confirm, Prompt

from .retro import CYAN, GREEN, THEME

CONFIG_DIR = os.path.expanduser("~/.talk2me")

# Keys the wizard may write; anything else in the file is ignored on load so
# a hand-edited or future-version file can't crash argument parsing.
ALLOWED_KEYS = ("model", "stt", "voice", "barge_in", "gated", "cwd", "save_dir")

# Flags that mean the caller already knows what they want — a first launch
# with any of these skips the wizard instead of interrogating a power user.
IDENTITY_FLAGS = (
    "--model", "--stt", "--voice", "--setup", "--text", "--list-devices",
    "--plain", "--help", "-h",
)


def config_path() -> str:
    return os.environ.get("TALK2ME_CONFIG") or os.path.join(
        CONFIG_DIR, "config.json"
    )


def load_saved_config() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return {k: data[k] for k in ALLOWED_KEYS if k in data}


def save_config(cfg: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: cfg[k] for k in ALLOWED_KEYS if k in cfg}, fh, indent=2)
        fh.write("\n")


def should_run_first_time(argv: list[str]) -> bool:
    """A brand-new interactive user with no opinions on the command line."""
    if os.path.exists(config_path()):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    return not any(flag in argv for flag in IDENTITY_FLAGS)


def _ava_installed() -> bool:
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=10
        ).stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return "Ava (Premium)" in out


def _parakeet_available() -> bool:
    try:
        import parakeet_mlx  # noqa: F401

        return True
    except ImportError:
        return False


def run_wizard(existing: dict | None = None) -> dict:
    """Interactive setup. Returns the config dict (already saved)."""
    prev = existing or {}
    console = Console(theme=THEME, highlight=False)
    p = console.print

    p()
    p(f"[bold {GREEN}]talk2me setup[/] — six questions, saved forever. "
      f"Re-run any time with [bold {CYAN}]t2m --setup[/] (or Ctrl-T mid-session).",
      )
    p()

    # 1 — the brain
    p(f"[bold {CYAN}]1 · the brain[/]  Which Claude model answers you. "
      "[dim]default = whatever your `claude` CLI is set to; haiku is the "
      "cheap fast one for casual chat.[/]")
    brain = Prompt.ask(
        "  brain",
        choices=["default", "haiku", "sonnet", "opus", "custom"],
        default=prev.get("model") and "custom" or "default",
    )
    if brain == "custom":
        model = Prompt.ask(
            "  model name", default=prev.get("model") or "claude-opus-4-6"
        )
    else:
        model = None if brain == "default" else brain

    # 2 — the ears
    p()
    p(f"[bold {CYAN}]2 · the ears[/]  Speech-to-text engine. "
      "[dim]whisper: runs everywhere, CPU. parakeet: Apple-Silicon GPU — "
      "faster AND more accurate, ~2 GB RAM, English-only.[/]")
    stt = Prompt.ask(
        "  ears", choices=["whisper", "parakeet"],
        default=prev.get("stt") or "whisper",
    )
    if stt == "parakeet" and not _parakeet_available():
        if Confirm.ask(
            "  parakeet isn't installed — install it now? (pip, one-time; "
            "the model downloads on first run)",
            default=True,
        ):
            rc = subprocess.call(
                [sys.executable, "-m", "pip", "install", "parakeet-mlx"]
            )
            if rc != 0:
                p("  [warn]install failed — using whisper for now "
                  "(switch later with --stt parakeet)[/]")
                stt = "whisper"
        else:
            stt = "whisper"

    # 3 — the voice
    p()
    p(f"[bold {CYAN}]3 · the voice[/]  What it sounds like. "
      "[dim]Recommended: Ava (Premium) — a voice from this decade.[/]")
    voice: str | None = prev.get("voice")
    if sys.platform == "darwin":
        if _ava_installed():
            picked = Prompt.ask(
                "  voice", default=voice or "Ava (Premium)"
            ).strip()
            voice = picked or None
        else:
            p("  Ava (Premium) isn't downloaded yet. Get it free: System "
              "Settings → Accessibility → Spoken Content → Read & Speak → "
              "ⓘ next to System voice → download [bold]Ava (Premium)[/].")
            if Confirm.ask(
                "  use the plain system voice for now? (pick Ava later with "
                "--voice)", default=True,
            ):
                voice = None
    else:
        voice = None  # `say` voices are macOS; kitten TTS is the alternative

    # 4 — barge-in
    p()
    p(f"[bold {CYAN}]4 · interrupting[/]  Barge-in keeps the mic live while "
      "it talks — start speaking and it shuts up and listens, like a person. "
      "[dim]Wants headphones; on open-air speakers it auto-downgrades so it "
      "doesn't argue with its own echo.[/]")
    barge_in = Confirm.ask(
        "  barge-in on? (recommended)", default=prev.get("barge_in", True)
    )

    # 5 — tools
    p()
    p(f"[bold {CYAN}]5 · tools[/]  auto-approve: the agent just works, like "
      "the Claude app — with the catastrophic stuff (sudo, force-push, "
      "rm -rf) hard-blocked in every mode. gated: it asks out loud before "
      "any non-read tool and you say approve/deny.")
    tools = Prompt.ask(
        "  tools",
        choices=["auto-approve", "gated"],
        default="gated" if prev.get("gated") else "auto-approve",
    )

    # 6 — where it works
    p()
    p(f"[bold {CYAN}]6 · the folder[/]  What the agent works on. "
      "[dim]Enter = no pin: it works on whatever folder you launch it from "
      "(most people want this). Type a path to pin one project forever — "
      "e.g. a notes vault you always want it pointed at.[/]")
    _FOLLOW = "wherever you launch it"
    cwd: str | None = prev.get("cwd")
    for _ in range(3):
        raw = Prompt.ask("  folder", default=cwd or _FOLLOW).strip()
        if not raw or raw == _FOLLOW:
            cwd = None
            break
        candidate = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(candidate):
            cwd = candidate
            break
        p(f"  [warn]not a folder:[/] {candidate}")
    else:
        cwd = None

    # 7 — transcripts
    p()
    save_dir: str | None = prev.get("save_dir")
    if Confirm.ask(
        "  save a markdown transcript of every session?",
        default=bool(save_dir),
    ):
        save_dir = Prompt.ask(
            "  transcripts folder", default=save_dir or "~/talk2me-logs"
        )
    else:
        save_dir = None

    cfg = {
        "model": model,
        "stt": stt,
        "voice": voice,
        "barge_in": barge_in,
        "gated": tools == "gated",
        "cwd": cwd,
        "save_dir": save_dir,
    }
    save_config(cfg)
    p()
    p(f"[bold {GREEN}]saved[/] → {config_path()}  "
      "[dim](flags always override; Ctrl-T or --setup to change)[/]")
    p()
    return cfg

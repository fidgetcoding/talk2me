"""First-run setup — pick the brain, ears, voice, and manners once, keep them.

Runs automatically on a first interactive voice launch (no saved config, no
identity flags on the command line), on demand via `t2m --setup`, and mid-
session via Ctrl-T (macOS). Answers land in ~/.talk2me/config.json and become
the DEFAULTS for every launch; explicit CLI flags always win over the file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from rich.console import Console
from rich.prompt import Confirm, Prompt

from .retro import CYAN, GREEN, THEME

CONFIG_DIR = os.path.expanduser("~/.talk2me")

# Keys the wizard may write; anything else in the file is ignored on load so
# a hand-edited or future-version file can't crash argument parsing.
ALLOWED_KEYS = (
    "model", "stt", "voice", "barge_in", "gated", "cwd", "save_dir",
    "language", "whisper_model", "backend_base_url", "backend_auth_env",
    "agent",
)

# Alternate brains that publish Anthropic-compatible endpoints, officially
# documented for use with the Claude Code CLI — same agent, different mind.
# (key env var, base url, example model)
PROVIDERS = {
    "kimi": ("MOONSHOT_API_KEY", "https://api.moonshot.ai/anthropic", "kimi-k2-0905-preview"),
    "glm": ("GLM_API_KEY", "https://open.bigmodel.cn/api/anthropic", "glm-4.6"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/anthropic", "deepseek-chat"),
}

# "opus 4.6" -> "claude-opus-4-6": people speak versions, the CLI wants ids.
_MODEL_SHORTHAND = re.compile(r"^(haiku|sonnet|opus)[ .\-]?(\d)[.\-](\d)$", re.IGNORECASE)


def normalize_model(text: str) -> str | None:
    t = text.strip()
    if not t or t.lower() == "default":
        return None
    m = _MODEL_SHORTHAND.match(t)
    if m:
        return f"claude-{m.group(1).lower()}-{m.group(2)}-{m.group(3)}"
    return t

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


def _installed_voices() -> list[str]:
    """Names from `say -v ?`, best first: Premium, then Enhanced, then stock."""
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=10
        ).stdout
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s{2,}[a-z]{2}[_-]", line)
        if m:
            names.append(m.group(1).strip())
    rank = lambda n: 0 if "(Premium)" in n else 1 if "(Enhanced)" in n else 2  # noqa: E731
    return sorted(dict.fromkeys(names), key=lambda n: (rank(n), n))


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
    p(f"[bold {CYAN}]1 · the brain[/]  Which coding agent answers you. "
      "[dim]claude = the Claude Code CLI (default). codex = OpenAI's Codex "
      "CLI (needs `codex login`). kimi / glm / deepseek run through the "
      "Claude agent via their official Anthropic-compatible endpoints — you "
      "bring an API key. custom = any compatible endpoint.[/]")
    provider_default = "claude"
    if prev.get("agent") == "codex":
        provider_default = "codex"
    elif prev.get("backend_base_url"):
        for name, (_, url, _m) in PROVIDERS.items():
            if prev["backend_base_url"] == url:
                provider_default = name
                break
        else:
            provider_default = "custom"
    provider = Prompt.ask(
        "  provider",
        choices=["claude", "codex", "kimi", "glm", "deepseek", "custom"],
        default=provider_default,
    )
    base_url: str | None = None
    auth_env: str | None = None
    agent = "codex" if provider == "codex" else "claude"
    if provider == "codex":
        model = Prompt.ask(
            "  model (Enter = codex default)", default=prev.get("model") or ""
        ).strip() or None
        if not os.path.exists(os.path.expanduser("~/.codex/auth.json")):
            p("  [warn]heads-up:[/] codex isn't logged in — run "
              "[bold]codex login[/] before launching")
    elif provider == "claude":
        p("  [dim]Enter = your `claude` CLI's default. Shorthand works: "
          "haiku · sonnet · opus · opus 4.6 · or any full model id.[/]")
        model = normalize_model(
            Prompt.ask("  model", default=prev.get("model") or "default")
        )
    else:
        if provider == "custom":
            base_url = Prompt.ask(
                "  endpoint URL (Anthropic-compatible)",
                default=prev.get("backend_base_url") or "https://",
            )
            auth_env = Prompt.ask(
                "  NAME of the env var holding your API key",
                default=prev.get("backend_auth_env") or "MY_AGENT_API_KEY",
            ).strip()
            model = Prompt.ask("  model id", default=prev.get("model") or "")
            model = model.strip() or None
        else:
            auth_env, base_url, example = PROVIDERS[provider]
            model = Prompt.ask("  model id", default=prev.get("model") or example)
        if auth_env and not os.environ.get(auth_env):
            p(f"  [warn]heads-up:[/] {auth_env} isn't set yet — before "
              f"launching, run:  [bold]export {auth_env}=<your api key>[/]  "
              "(the key never lands in a file)")

    # 2 — the ears
    p()
    p(f"[bold {CYAN}]2 · the ears[/]  Speech-to-text engine. "
      "[dim]whisper: runs everywhere, CPU, any language. parakeet: "
      "Apple-Silicon GPU — faster AND more accurate, ~2 GB RAM, but "
      "English-only.[/]")
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
    language = prev.get("language") or "en"
    whisper_model: str | None = prev.get("whisper_model")
    if stt == "whisper":
        p("  [dim]language: a 2-letter code (en, es, de, fr, pt, ja…) or "
          "'auto' to detect per sentence. Voice commands (pause/wake/"
          "approve) stay English for now.[/]")
        language = Prompt.ask("  language", default=language).strip() or "en"
        if language != "en" and (whisper_model or "base.en").endswith(".en"):
            # .en models are English-only; swap to the multilingual sibling.
            whisper_model = (whisper_model or "base.en").removesuffix(".en")
            p(f"  [dim](using the multilingual model: {whisper_model})[/]")
    elif language != "en":
        p("  [warn]parakeet is English-only — keeping language=en[/]")
        language = "en"

    # 3 — the voice
    p()
    p(f"[bold {CYAN}]3 · the voice[/]  What it sounds like. "
      "[dim]Recommended: Ava (Premium) — a voice from this decade.[/]")
    voice: str | None = prev.get("voice")
    if sys.platform == "darwin":
        voices = _installed_voices()
        if voices:
            show = voices[:12]
            p("  installed on this Mac: " + " · ".join(
                f"[bold {GREEN}]{v}[/]" if "(Premium)" in v or "(Enhanced)" in v
                else v
                for v in show
            ) + (f" [dim](+{len(voices) - len(show)} more)[/]" if len(voices) > len(show) else ""))
        if "Ava (Premium)" not in voices:
            p("  Ava (Premium) isn't downloaded yet. Get it free: System "
              "Settings → Accessibility → Spoken Content → Read & Speak → "
              "ⓘ next to System voice → download [bold]Ava (Premium)[/] — "
              "then set it here later.")
        default_voice = voice or (
            "Ava (Premium)" if "Ava (Premium)" in voices else "system"
        )
        picked = Prompt.ask("  voice", default=default_voice).strip()
        voice = None if picked in ("", "system") else picked
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
        for _ in range(3):
            raw = Prompt.ask(
                "  transcripts folder", default=save_dir or "~/talk2me-logs"
            )
            candidate = os.path.expanduser(raw)
            # Prove it's writable NOW — a typo'd path must fail here with a
            # re-ask, not at launch with a traceback (live bug: a root-level
            # path hit the read-only filesystem and killed the session).
            try:
                os.makedirs(candidate, exist_ok=True)
            except OSError as exc:
                p(f"  [warn]can't create that folder:[/] {exc}")
                continue
            save_dir = raw
            break
        else:
            p("  [warn]no writable folder — transcripts off[/]")
            save_dir = None
    else:
        save_dir = None

    cfg = {
        "agent": agent,
        "model": model,
        "stt": stt,
        "voice": voice,
        "barge_in": barge_in,
        "gated": tools == "gated",
        "cwd": cwd,
        "save_dir": save_dir,
        "language": language,
        "whisper_model": whisper_model,
        "backend_base_url": base_url,
        "backend_auth_env": auth_env,
    }
    save_config(cfg)
    p()
    p(f"[bold {GREEN}]saved[/] → {config_path()}  "
      "[dim](flags always override; Ctrl-T or --setup to change)[/]")
    p()
    return cfg

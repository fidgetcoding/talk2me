#!/usr/bin/env python3
"""Claude Code Stop-hook speaker — hear Claude Code's replies inside the REAL
Claude Code TUI.

Wire it as a Stop hook in ~/.claude/settings.json:

    {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command",
        "command": "python3 /path/to/talk2me/scripts/claude-speak.py"}]}]}}

The hook receives Claude Code's payload on stdin, reads the session transcript,
extracts the LAST assistant message, strips the markdown, and speaks it with
macOS `say` — detached, so the hook returns instantly.

Gated hard by a toggle file so scheduled agents and background sessions never
speak at you:

    ~/.talk2me-speak         present = speak, absent = silent (default)

The toggle file's CONTENT can override the voice/rate: `Ava (Premium)|236`.
Stdlib only; zero talk2me imports — copy this single file anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

TOGGLE = os.path.expanduser("~/.talk2me-speak")
DEFAULT_VOICE = "Ava (Premium)"
DEFAULT_RATE = "236"
# A monster reply shouldn't monologue for five minutes; the screen has the rest.
MAX_SPOKEN_CHARS = 1200


def main() -> int:
    if not os.path.exists(TOGGLE):
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tpath = payload.get("transcript_path")
    if not tpath or not os.path.isfile(tpath):
        return 0
    text = _last_assistant_text(tpath)
    if not text:
        return 0
    # Speak each reply exactly ONCE, keyed by content — NOT by skipping
    # stop_hook_active turns. Other Stop hooks (e.g. a memory-save hook that
    # cancels the first stop) mean the only stop we ever see may carry
    # stop_hook_active=true; content-dedupe both survives that and prevents
    # the re-speak loop the flag was guarding against.
    session = str(payload.get("session_id") or "nosession")[:64]
    marker = os.path.join(
        tempfile.gettempdir(), f"talk2me-spoke-{session}.hash"
    )
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    try:
        if open(marker, encoding="utf-8").read().strip() == digest:
            return 0
    except OSError:
        pass
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(digest)
    except OSError:
        pass
    speech = _clean(text)
    if not speech:
        return 0
    if len(speech) > MAX_SPOKEN_CHARS:
        head = speech[:MAX_SPOKEN_CHARS].rsplit(". ", 1)[0]
        speech = head + ". That's the short version — the rest is on screen."
    voice, rate = _voice_config()
    # A new reply supersedes whatever is still being spoken.
    subprocess.run(["killall", "say"], capture_output=True)
    subprocess.Popen(
        ["say", "-v", voice, "-r", rate, "--", speech],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return 0


def _voice_config() -> tuple[str, str]:
    voice, rate = DEFAULT_VOICE, DEFAULT_RATE
    try:
        raw = open(TOGGLE, encoding="utf-8").read().strip()
    except Exception:
        return voice, rate
    if raw:
        parts = raw.split("|")
        if parts[0]:
            voice = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            rate = parts[1]
    return voice, rate


def _last_assistant_text(tpath: str) -> str | None:
    """Last assistant message's text blocks from a Claude Code transcript."""
    text = None
    try:
        with open(tpath, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                parts = [
                    b.get("text", "")
                    for b in (msg.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p).strip()
                if joined:
                    text = joined
    except OSError:
        return None
    return text


def _clean(text: str) -> str:
    """Markdown -> speakable prose. Code never gets read aloud."""
    text = re.sub(r"```.*?```", " Code is on screen. ", text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", lambda m: m.group(0).strip("`"), text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> label
    text = re.sub(r"^\s*[-•*]\s+", "", text, flags=re.M)  # bullet markers
    text = re.sub(r"^#+\s*", "", text, flags=re.M)  # headings
    text = re.sub(r"[*_#>|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


if __name__ == "__main__":
    sys.exit(main())

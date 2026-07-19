"""Plain-markdown session transcripts.

When `--save-dir` (or TALK2ME_SAVE_DIR) is set, every session appends to one
markdown file as it happens — what you said, what Claude answered, which tools
ran, what you approved. Human-readable, crash-safe (flushed per write), and
deliberately boring: no frontmatter, no schema, just the conversation.

(Claude Code separately keeps its own machine-format transcript per session
under ~/.claude/projects/… — that one powers `claude --resume`. This file is
the one you'd actually reread.)
"""

from __future__ import annotations

import os
import time


class SessionLog:
    def __init__(
        self,
        directory: str,
        *,
        model: str | None,
        stt: str,
        cwd: str | None,
    ) -> None:
        directory = os.path.expanduser(directory)
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        self.path = os.path.join(directory, f"t2m-{stamp}.md")
        self._dead = False
        started = time.strftime("%Y-%m-%d %I:%M %p")
        self._append(
            f"# talk2me session — {started}\n\n"
            f"- model: {model or 'claude default'}\n"
            f"- ears: {stt}\n"
            f"- working dir: {cwd or os.getcwd()}\n"
        )

    def user(self, text: str, kind: str = "") -> None:
        label = f"You ({kind})" if kind else "You"
        self._append(f"\n**{label}:** {text}\n")

    def assistant(self, text: str) -> None:
        if text.strip():
            self._append(f"\n**Claude:** {text.strip()}\n")

    def tool(self, name: str) -> None:
        self._append(f"- 🔧 {name}\n")

    def permission(self, tool_name: str, detail: str, allowed: bool) -> None:
        verdict = "APPROVED" if allowed else "DENIED"
        self._append(f"- 🔐 {verdict}: {tool_name} ({detail})\n")

    def event(self, text: str) -> None:
        self._append(f"- ℹ️ {text}\n")

    def _append(self, text: str) -> None:
        """Append + flush; a failing disk mutes the log, never the session."""
        if self._dead:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            self._dead = True
            print(f"[save-dir] transcript disabled: {exc}", flush=True)

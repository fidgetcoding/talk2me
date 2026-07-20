"""The retro renderer — the banner's look, live in the terminal.

Green prose on black, pink for the human, cyan chips for machinery, dotted
borders. Rich-backed, but only its boring parts (Console, Theme, Panel, Text);
no alternate screen, no curses, no full TUI — still a scrolling conversation.

Security invariant: agent/user/tool content NEVER passes through rich markup.
Every content-carrying line is built with Text.assemble (which treats strings
as literals), so a reply containing "[red]" renders as the five characters
[ r e d ] — locked by tests/test_retro.py.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import TYPE_CHECKING

from rich.box import Box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from .config import Config

# Banner-matched palette (talk2me.png): the lime green IS the brand, pink and
# cyan are the accents. Hex-pinned so terminal palettes can't drift it.
GREEN = "#a8dd00"
PINK = "#ff5fd7"
CYAN = "#5fd7ff"

THEME = Theme(
    {
        "agent": GREEN,
        "frame": GREEN,
        "you": f"bold {PINK}",
        "you.text": PINK,
        "chip": CYAN,
        "info": CYAN,
        "tool.name": f"bold {GREEN}",
        "detail": "grey58",
        "quiet": "grey58",
        "warn": f"bold {PINK}",
        "ok": f"bold {GREEN}",
    }
)

# The launch banner. Skipped automatically when the terminal is too narrow —
# a wrapped banner is worse than no banner.
BANNER = """\
████████╗ █████╗ ██╗     ██╗  ██╗██████╗ ███╗   ███╗███████╗
╚══██╔══╝██╔══██╗██║     ██║ ██╔╝╚════██╗████╗ ████║██╔════╝
   ██║   ███████║██║     █████╔╝  █████╔╝██╔████╔██║█████╗
   ██║   ██╔══██║██║     ██╔═██╗ ██╔═══╝ ██║╚██╔╝██║██╔══╝
   ██║   ██║  ██║███████╗██║  ██╗███████╗██║ ╚═╝ ██║███████╗
   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝╚══════╝"""
BANNER_WIDTH = max(len(line) for line in BANNER.splitlines())

# Dotted box echoing the banner's ┄┄┄ borders.
DOTTED = Box(
    "┌┄┬┐\n"
    "┆ ┆┆\n"
    "├┄┼┤\n"
    "┆ ┆┆\n"
    "├┄┼┤\n"
    "├┄┼┤\n"
    "┆ ┆┆\n"
    "└┄┴┘\n"
)


def _elapsed_label(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


class _WorkPanel:
    """State for one tool burst's Live panel: spinner, last 5 tools, count,
    elapsed. Rendered fresh on every refresh tick, so the clock and spinner
    animate for free."""

    KEEP = 5

    def __init__(self) -> None:
        self.entries: deque[tuple[str, str]] = deque(maxlen=self.KEEP)
        self.count = 0
        self.t0 = time.monotonic()
        self._spinner = Spinner("dots", style="chip")

    def add(self, name: str, detail: str) -> None:
        self.entries.append((name, detail))
        self.count += 1

    def upgrade(self, name: str, detail: str) -> None:
        """Attach detail to the most recent entry with this name (the stream
        path announced it bare; the full message brought the arguments)."""
        for i in range(len(self.entries) - 1, -1, -1):
            if self.entries[i][0] == name and not self.entries[i][1]:
                self.entries[i] = (name, detail)
                return

    def summary(self) -> str:
        plural = "s" if self.count != 1 else ""
        return (
            f"   ⚙ {self.count} tool call{plural} · "
            f"{_elapsed_label(time.monotonic() - self.t0)}"
        )

    def __rich__(self) -> Panel:
        plural = "s" if self.count != 1 else ""
        self._spinner.update(
            text=Text.assemble(
                (f" {self.count} tool call{plural}", "chip"),
                (f" · {_elapsed_label(time.monotonic() - self.t0)}", "quiet"),
            )
        )
        lines: list = [self._spinner]
        for name, detail in self.entries:
            line = Text.assemble(("⚙ ", "chip"), (name, "tool.name"))
            if detail:
                line.append("  ")
                line.append(detail, style="detail")
            lines.append(line)
        return Panel(
            Group(*lines),
            box=DOTTED,
            border_style="frame",
            title=Text("working", style="chip"),
            title_align="left",
            expand=False,
            padding=(0, 1),
        )


class RetroRenderer:
    """Renderer with the retro skin. Same vocabulary as PlainRenderer; every
    line semantically identical, just dressed.

    The tool phase runs inside ONE rich Live region (the work panel). The
    interleave discipline that keeps Live and streamed prose from corrupting
    each other: every non-tool output first collapses the panel into a
    permanent one-line summary, then prints. agent_delta streams with end=""
    (a partial line), so the panel must never be alive while prose flows."""

    def __init__(self, console: Console | None = None) -> None:
        if console is None:
            console = Console(theme=THEME, highlight=False)
        else:  # injected by tests — make sure the palette rides along
            console.push_theme(THEME)
        self.console = console
        self._live: Live | None = None
        self._panel: _WorkPanel | None = None
        # True while the agent's streamed prose has left the cursor mid-line —
        # the panel must terminate that line before Live takes the screen.
        self._inline = False
        # Which stream currently owns the line: "" / "prose" / "think" —
        # drives the newline + 🧠 prefix when thinking and prose interleave.
        self._stream = ""

    def _collapse(self) -> None:
        """Fold an active work panel into its permanent one-line summary.
        No-op when no panel is live. Never raises: a dead console must not
        mask the caller's real output or exception."""
        if self._live is None:
            return
        live, panel = self._live, self._panel
        self._live = self._panel = None
        try:
            live.stop()  # transient=True wipes the panel from the screen
        except Exception:
            return
        if panel is not None:
            self.console.print(
                panel.summary(), style="quiet", markup=False, highlight=False
            )

    # ---- startup ---------------------------------------------------------

    def loading_ears(self) -> None:
        self.console.print("(loading the ears…)", style="quiet", markup=False)

    def startup(self, cfg: Config) -> None:
        if self.console.width >= BANNER_WIDTH:
            self.console.print()
            self.console.print(BANNER, style="agent", markup=False, highlight=False)
        home = os.path.expanduser("~")

        def tilde(path: str) -> str:
            return path.replace(home, "~", 1) if path.startswith(home) else path

        tools_mode = (
            "auto-approve ⚡"
            if "bypass" in cfg.permission_mode.lower()
            else "gated (spoken approvals)"
        )
        if getattr(cfg, "echo_guard", False):
            barge = "ON — voice-locked talk-over (speakers; talk ~1s to cut)"
        elif cfg.barge_in:
            barge = "ON — talk over it anytime"
        elif getattr(cfg, "barge_downgraded", False):
            barge = "gaps only (speakers — it mutes its ears ONLY while speaking)"
        else:
            barge = "off"
        rows = [
            ("brain", cfg.model or "claude default"),
            ("ears", cfg.stt),
            ("voice", f"{cfg.voice or 'system'} @{cfg.rate_wpm or 'default'}wpm"),
            ("barge-in", barge),
            ("tools", tools_mode),
            ("working on", tilde(cfg.cwd or os.getcwd())),
        ]
        if getattr(cfg, "voice_lock", False):
            lock_row = (
                "observing (weak calibration — nothing blocked; re-enroll "
                "to enforce)"
                if getattr(cfg, "voice_lock_observing", False)
                else 'ON — solo (say "team session" to open up)'
            )
            rows.insert(4, ("voice-lock", lock_row))
        if cfg.save_dir:
            rows.append(
                ("saves to", tilde(os.path.expanduser(cfg.save_dir)))
            )
        body = Text()
        for i, (key, value) in enumerate(rows):
            if i:
                body.append("\n")
            body.append(f"{key:<11}", style="chip")
            body.append(str(value), style="agent")
        self.console.print()
        self.console.print(
            Panel(
                body,
                box=DOTTED,
                border_style="frame",
                title=Text("talk2me", style="ok"),
                title_align="left",
                subtitle=Text("made by @fidgetcoding :)", style="quiet"),
                subtitle_align="right",
                expand=False,
                padding=(0, 2),
            )
        )
        hint = (
            "ready — start talking. Ctrl-C to quit · Ctrl-T to change settings."
            if sys.platform == "darwin"
            else "ready — start talking. Ctrl-C to quit."
        )
        self.console.print(hint, style="agent", markup=False)
        if cfg.half_duplex:
            hint = (
                "(speakers: you're heard any time it isn't mid-sentence — "
                "thinking, tool runs, between turns. Headphones add "
                "talk-over-its-voice; nothing else changes)"
                if getattr(cfg, "barge_downgraded", False)
                else "(half-duplex: talking over the agent mid-speech is "
                "ignored — run with --barge-in and headphones to interrupt it)"
            )
            self.console.print(hint, style="quiet", markup=False)

    def transcript_path(self, path: str) -> None:
        self.console.print(
            Text.assemble(("📝 saving transcript to ", "info"), (path, "detail"))
        )

    def speaker_downgrade(self) -> None:
        self._collapse()
        self.console.print(
            "🔈 speakers on the output — barge-in off for this session so I "
            "don't argue with my own echo. Plug in headphones to interrupt me.",
            style="warn",
            markup=False,
        )

    def device_error(self, msg: str) -> None:
        self.console.print(
            Text.assemble(
                ("[device] ", "warn"),
                (msg, "warn"),
                ("\n\nRun `talk2me --list-devices` to see options.", "quiet"),
            )
        )

    # ---- the loop --------------------------------------------------------

    def listening(self, *, nl: bool = True) -> None:
        self._collapse()
        if nl:
            self.console.print()
        self.console.print("🎧 listening…", style="info", markup=False)

    def noise_ignored(self) -> None:
        self._collapse()
        self.console.print(
            "   (ignored — transcription noise)", style="quiet", markup=False
        )

    def paused(self) -> None:
        self._collapse()
        self.console.print()
        self.console.print(
            "⏸  paused — say 'wake up' when you need me", style="info", markup=False
        )

    def still_paused(self) -> None:
        self._collapse()
        self.console.print(
            "⏸  still paused — say 'wake up'", style="quiet", markup=False
        )

    def paused_ignored(self, text: str) -> None:
        self._collapse()
        self.console.print(
            Text.assemble(("   (paused — ignored: ", "quiet"), (text, "quiet"), (")", "quiet"))
        )

    def awake(self) -> None:
        self._collapse()
        self.console.print()
        self.console.print("▶️  awake — listening again", style="info", markup=False)

    def waiting_for_rest(self) -> None:
        self._collapse()
        self.console.print(
            "   (…waiting for the rest)", style="quiet", markup=False
        )

    def noise_resend(self) -> None:
        self._collapse()
        self.console.print()
        self.console.print(
            "   (noise interrupt — repeating your question)",
            style="quiet",
            markup=False,
        )

    def you(self, text: str, kind: str = "") -> None:
        self._collapse()
        label = f"▸ you ({kind})" if kind else "▸ you"
        self.console.print()
        self.console.print(
            Text.assemble((label, "you"), ("  ", ""), (text, "you.text"))
        )

    def agent_begin(self) -> None:
        # The 🤖 prefix is deferred to the first prose chunk: a turn that
        # opens with thinking or goes straight to tools must not strand a
        # lone robot on its own line (live-observed in v2.0.0).
        self._collapse()
        self._inline = False
        self._stream = "pending"

    def agent_delta(self, text: str) -> None:
        # Prose can resume right after a tool burst with no fresh
        # agent_begin — the panel must fold before a single character lands.
        self._collapse()
        if self._stream == "think":
            self.console.print()  # end the thinking line before prose
            self._stream = "pending"
        if self._stream != "prose":
            self.console.print("🤖 ", style="agent", end="", markup=False)
            self._stream = "prose"
        self._inline = True
        # Streamed fragments: soft_wrap leaves wrapping to the terminal so a
        # chunk boundary can never hard-break a word mid-line. markup=False +
        # highlight=False: agent prose must never execute markup NOR get
        # bracket-chasing auto-highlights.
        self.console.print(
            text, style="agent", end="", markup=False, highlight=False,
            soft_wrap=True,
        )

    def agent_end(self) -> None:
        self._collapse()
        if self._inline:  # terminate a streamed line; tool-only turns have none
            self.console.print()
        self._inline = False
        self._stream = ""

    def thinking(self, text: str) -> None:
        # The agent's reasoning stream: shown dim, never spoken. Same
        # interleave rules as prose — fold the panel, own the line.
        self._collapse()
        if self._stream != "think":
            if self._inline:
                self.console.print()
            self.console.print("🧠 ", style="quiet", end="", markup=False)
            self._stream = "think"
            self._inline = True
        self.console.print(
            text, style="italic grey58", end="", markup=False,
            highlight=False, soft_wrap=True,
        )

    # ---- machinery -------------------------------------------------------

    def tool(
        self, name: str, detail: str = "", *, body: str = "", follow_on: bool = False
    ) -> None:
        if follow_on:
            if detail:
                if self._panel is not None:
                    self._panel.upgrade(name, detail)
                else:  # panel already collapsed — linear follow-on, like Plain
                    self.console.print(
                        Text.assemble(("      ↳ ", "quiet"), (detail, "detail"))
                    )
            # The work itself — a permanent code card. While the panel is
            # live, rich weaves this above the Live region.
            self._code_card(name, detail, body)
            return
        if self._inline:
            # Streamed prose left the cursor mid-line; end it before Live
            # takes over the bottom of the screen.
            self.console.print()
            self._inline = False
            self._stream = ""
        if self._live is None:
            self._panel = _WorkPanel()
            live = Live(
                self._panel,
                console=self.console,
                refresh_per_second=8,
                transient=True,
                # Never hijack the process streams: rich's default redirect
                # swallows out-of-seam prints (and in tests, entire suites)
                # into the console file. The panel owns its region, not stdio.
                redirect_stdout=False,
                redirect_stderr=False,
            )
            try:
                live.start()
                self._live = live
            except Exception:
                self._live = self._panel = None  # fall back to linear lines
        if self._panel is not None:
            self._panel.add(name, detail)
        if self._live is None:
            # Live unavailable (or failed to start): Phase-3 linear line.
            line = Text.assemble(("\n   ⚙ ", "chip"), (name, "tool.name"))
            if detail:
                line.append("  ")
                line.append(detail, style="detail")
            self.console.print(line)
        self._code_card(name, detail, body)

    def _code_card(self, name: str, detail: str, body: str) -> None:
        """A permanent, syntax-highlighted card of the tool's actual work —
        the code being written, the diff, the command. Content never touches
        rich markup; the highlighter is a lexer, not a tag parser."""
        if not body:
            return
        inner: object
        try:
            lexer = Syntax.guess_lexer(detail or name, body)
            inner = Syntax(body, lexer, background_color="default", word_wrap=True)
        except Exception:
            inner = Text(body, style="detail")
        title = Text.assemble((name, "chip"))
        if detail:
            title.append(" · ")
            title.append(detail, style="detail")
        self.console.print(
            Panel(
                inner,
                box=DOTTED,
                border_style="quiet",
                title=title,
                title_align="left",
                expand=False,
                padding=(0, 1),
            )
        )

    def working(self, tool_count: int) -> None:
        if self._live is not None:
            return  # the panel's own clock and spinner already show life
        plural = "s" if tool_count != 1 else ""
        self.console.print(
            f"   ⚙ still working… ({tool_count} tool call{plural} so far)",
            style="quiet",
            markup=False,
            highlight=False,
        )

    def barge_label(self, spoke_any: bool) -> None:
        self._collapse()
        label = "[barge-in] listening…" if spoke_any else "[go on…]"
        self.console.print()
        self.console.print(f"   {label}", style="chip", markup=False)

    def permission_ask(self, tool: str, detail: str) -> None:
        self._collapse()
        self.console.print()
        self.console.print(
            Text.assemble(
                ("   [permission] ", "warn"), (tool, "tool.name"), (": ", "quiet"),
                (detail, "detail"),
            )
        )

    def permission_heard(self, heard: str, decision: str | None) -> None:
        self._collapse()
        self.console.print(
            Text.assemble(
                ("   [permission] you: ", "chip"),
                (heard, "you.text"),
                (" -> ", "quiet"),
                (decision or "unclear", "chip"),
            )
        )

    def permission_verdict(self, tool: str, allowed: bool) -> None:
        self._collapse()
        verdict = ("APPROVED", "ok") if allowed else ("DENIED", "warn")
        self.console.print(
            Text.assemble(
                ("   [permission] ", "chip"), verdict, (": ", "quiet"),
                (tool, "tool.name"),
            )
        )

    # ---- errors / debug / teardown ---------------------------------------

    def status_note(self, text: str) -> None:
        self._collapse()
        self.console.print(
            f"   ({text})", style="quiet", markup=False, highlight=False
        )

    def error(self, msg: str) -> None:
        self._collapse()
        self.console.print()
        self.console.print(msg, style="warn", markup=False, highlight=False)

    def debug(self, msg: str, *, nl: bool = False) -> None:
        self._collapse()
        if nl:
            self.console.print()
        self.console.print(
            f"  {msg}", style="quiet", markup=False, highlight=False
        )

    def close(self) -> None:
        """Restore the terminal: fold any live panel (Live.stop() gives the
        screen back). Safe to call twice; never raises past _collapse."""
        self._collapse()

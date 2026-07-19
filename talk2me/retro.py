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
from typing import TYPE_CHECKING

from rich.box import Box
from rich.console import Console
from rich.panel import Panel
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


class RetroRenderer:
    """Renderer with the retro skin. Same vocabulary as PlainRenderer; every
    line semantically identical, just dressed."""

    def __init__(self, console: Console | None = None) -> None:
        if console is None:
            console = Console(theme=THEME, highlight=False)
        else:  # injected by tests — make sure the palette rides along
            console.push_theme(THEME)
        self.console = console

    # ---- startup ---------------------------------------------------------

    def loading_ears(self) -> None:
        self.console.print("(loading the ears…)", style="quiet", markup=False)

    def startup(self, cfg: Config) -> None:
        tools_mode = (
            "auto-approve ⚡"
            if "bypass" in cfg.permission_mode.lower()
            else "gated (spoken approvals)"
        )
        rows = [
            ("brain", cfg.model or "claude default"),
            ("ears", cfg.stt),
            ("voice", f"{cfg.voice or 'system'} @{cfg.rate_wpm or 'default'}wpm"),
            ("barge-in", "ON" if cfg.barge_in else "off"),
            ("tools", tools_mode),
            ("folder", cfg.cwd or os.getcwd()),
        ]
        body = Text()
        for i, (key, value) in enumerate(rows):
            if i:
                body.append("\n")
            body.append(f"{key:<9}", style="chip")
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
        self.console.print(
            "ready — start talking. Ctrl-C to quit.", style="agent", markup=False
        )
        if cfg.half_duplex:
            self.console.print(
                "(half-duplex: talking over the agent mid-speech is ignored "
                "— run with --barge-in and headphones to interrupt it)",
                style="quiet",
                markup=False,
            )

    def transcript_path(self, path: str) -> None:
        self.console.print(
            Text.assemble(("📝 saving transcript to ", "info"), (path, "detail"))
        )

    def speaker_downgrade(self) -> None:
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
        if nl:
            self.console.print()
        self.console.print("🎧 listening…", style="info", markup=False)

    def noise_ignored(self) -> None:
        self.console.print(
            "   (ignored — transcription noise)", style="quiet", markup=False
        )

    def paused(self) -> None:
        self.console.print()
        self.console.print(
            "⏸  paused — say 'wake up' when you need me", style="info", markup=False
        )

    def still_paused(self) -> None:
        self.console.print(
            "⏸  still paused — say 'wake up'", style="quiet", markup=False
        )

    def paused_ignored(self, text: str) -> None:
        self.console.print(
            Text.assemble(("   (paused — ignored: ", "quiet"), (text, "quiet"), (")", "quiet"))
        )

    def awake(self) -> None:
        self.console.print()
        self.console.print("▶️  awake — listening again", style="info", markup=False)

    def waiting_for_rest(self) -> None:
        self.console.print(
            "   (…waiting for the rest)", style="quiet", markup=False
        )

    def noise_resend(self) -> None:
        self.console.print()
        self.console.print(
            "   (noise interrupt — repeating your question)",
            style="quiet",
            markup=False,
        )

    def you(self, text: str, kind: str = "") -> None:
        label = f"▸ you ({kind})" if kind else "▸ you"
        self.console.print()
        self.console.print(
            Text.assemble((label, "you"), ("  ", ""), (text, "you.text"))
        )

    def agent_begin(self) -> None:
        self.console.print("🤖 ", style="agent", end="", markup=False)

    def agent_delta(self, text: str) -> None:
        # Streamed fragments: soft_wrap leaves wrapping to the terminal so a
        # chunk boundary can never hard-break a word mid-line. markup=False +
        # highlight=False: agent prose must never execute markup NOR get
        # bracket-chasing auto-highlights.
        self.console.print(
            text, style="agent", end="", markup=False, highlight=False,
            soft_wrap=True,
        )

    def agent_end(self) -> None:
        self.console.print()

    # ---- machinery -------------------------------------------------------

    def tool(self, name: str, detail: str = "", *, follow_on: bool = False) -> None:
        if follow_on:
            if detail:
                self.console.print(
                    Text.assemble(("      ↳ ", "quiet"), (detail, "detail"))
                )
            return
        line = Text.assemble(("\n   ⚙ ", "chip"), (name, "tool.name"))
        if detail:
            line.append_text(Text.assemble(("  ", ""), (detail, "detail")))
        self.console.print(line)

    def working(self, tool_count: int) -> None:
        plural = "s" if tool_count != 1 else ""
        self.console.print(
            f"   ⚙ still working… ({tool_count} tool call{plural} so far)",
            style="quiet",
            markup=False,
            highlight=False,
        )

    def barge_label(self, spoke_any: bool) -> None:
        label = "[barge-in] listening…" if spoke_any else "[go on…]"
        self.console.print()
        self.console.print(f"   {label}", style="chip", markup=False)

    def permission_ask(self, tool: str, detail: str) -> None:
        self.console.print()
        self.console.print(
            Text.assemble(
                ("   [permission] ", "warn"), (tool, "tool.name"), (": ", "quiet"),
                (detail, "detail"),
            )
        )

    def permission_heard(self, heard: str, decision: str | None) -> None:
        self.console.print(
            Text.assemble(
                ("   [permission] you: ", "chip"),
                (heard, "you.text"),
                (" -> ", "quiet"),
                (decision or "unclear", "chip"),
            )
        )

    def permission_verdict(self, tool: str, allowed: bool) -> None:
        verdict = ("APPROVED", "ok") if allowed else ("DENIED", "warn")
        self.console.print(
            Text.assemble(
                ("   [permission] ", "chip"), verdict, (": ", "quiet"),
                (tool, "tool.name"),
            )
        )

    # ---- errors / debug / teardown ---------------------------------------

    def error(self, msg: str) -> None:
        self.console.print()
        self.console.print(msg, style="warn", markup=False, highlight=False)

    def debug(self, msg: str, *, nl: bool = False) -> None:
        if nl:
            self.console.print()
        self.console.print(
            f"  {msg}", style="quiet", markup=False, highlight=False
        )

    def close(self) -> None:
        pass

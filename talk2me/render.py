"""The renderer seam — every user-facing line the voice loop prints.

The orchestrator and the voice entrypoint never call print() for conversation
output; they call a Renderer. PlainRenderer reproduces the launch build's
output byte-for-byte (locked by tests/test_render.py snapshots), so the seam
is a pure refactor: same voice loop, same screen, one indirection.

Deliberately out of scope, by design:
- `segment.py`'s --debug ▶/⏹ prints (diagnostic, keeps the segmenter
  dependency-free)
- text mode's REPL prints in __main__ (a typed REPL stays plain forever)
- the final "bye." in main() (printed after the loop is torn down)
- SessionLog's own disabled-transcript warning (fires from inside the log)
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .config import Config


class Renderer(Protocol):
    """Everything the voice loop can put on the screen.

    Implementations own ALL decoration (emoji, indents, leading newlines,
    color); callers pass semantic content only. close() MUST restore the
    terminal to a sane state and be safe to call twice.
    """

    def loading_ears(self) -> None: ...
    def startup(self, cfg: Config) -> None: ...
    def transcript_path(self, path: str) -> None: ...
    def speaker_downgrade(self) -> None: ...
    def device_error(self, msg: str) -> None: ...
    def listening(self, *, nl: bool = True) -> None: ...
    def noise_ignored(self) -> None: ...
    def paused(self) -> None: ...
    def still_paused(self) -> None: ...
    def paused_ignored(self, text: str) -> None: ...
    def awake(self) -> None: ...
    def waiting_for_rest(self) -> None: ...
    def noise_resend(self) -> None: ...
    def you(self, text: str, kind: str = "") -> None: ...
    def agent_begin(self) -> None: ...
    def agent_delta(self, text: str) -> None: ...
    def agent_end(self) -> None: ...
    def thinking(self, text: str) -> None: ...
    def tool(
        self, name: str, detail: str = "", *, body: str = "", follow_on: bool = False
    ) -> None: ...
    def working(self, tool_count: int) -> None: ...
    def barge_label(self, spoke_any: bool) -> None: ...
    def permission_ask(self, tool: str, detail: str) -> None: ...
    def permission_heard(self, heard: str, decision: str | None) -> None: ...
    def permission_verdict(self, tool: str, allowed: bool) -> None: ...
    def status_note(self, text: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def debug(self, msg: str, *, nl: bool = False) -> None: ...
    def typeahead(self, buffer: str) -> None: ...
    def close(self) -> None: ...


# Renderer methods that print. Wrapped at construction so the in-progress
# typeahead line is erased before ANY output lands — otherwise every print
# would append after the ⌨ line and litter the scrollback with stale
# fragments (the v2.4 garbled-typing mess, from the other direction).
_PRINTING_METHODS = (
    "loading_ears", "startup", "transcript_path", "speaker_downgrade",
    "device_error", "listening", "noise_ignored", "paused", "still_paused",
    "paused_ignored", "awake", "waiting_for_rest", "noise_resend", "you",
    "agent_begin", "agent_delta", "agent_end", "thinking", "tool", "working",
    "barge_label", "permission_ask", "permission_heard", "permission_verdict",
    "status_note", "error", "debug",
)


def _wrap_for_typeahead(renderer) -> None:
    """Instance-rebind every printing method to erase the typeahead line
    first. A no-op unless a line is actually visible, so snapshot-locked
    output is untouched when nobody is typing."""

    def make(orig):
        def wrapped(*args, **kwargs):
            renderer._ta_clear()
            return orig(*args, **kwargs)

        return wrapped

    for name in _PRINTING_METHODS:
        orig = getattr(renderer, name, None)
        if orig is not None:
            setattr(renderer, name, make(orig))


class PlainRenderer:
    """The launch build's output, verbatim. Also the automatic fallback for
    pipes, CI, NO_COLOR, and a missing rich — a broken paint job must never
    mute the product."""

    def __init__(self) -> None:
        self._ta_visible = False
        self._ta_buffer = ""
        # Mid-line prose streaming (agent_delta) — never redraw over it.
        self._ta_inline = False
        _wrap_for_typeahead(self)

    def _ta_clear(self) -> None:
        if self._ta_visible:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
            self._ta_visible = False

    def typeahead(self, buffer: str) -> None:
        """Draw (or erase) the in-progress typed line at the cursor row."""
        self._ta_buffer = buffer
        if not sys.stdout.isatty():
            return
        if self._ta_inline:
            return  # prose owns the line; the buffer shows at next boundary
        sys.stdout.write("\r\x1b[2K")
        if buffer:
            sys.stdout.write(f"⌨ {buffer[-160:]}")
            self._ta_visible = True
        else:
            self._ta_visible = False
        sys.stdout.flush()

    def loading_ears(self) -> None:
        print("(loading the ears…)", flush=True)

    def startup(self, cfg: Config) -> None:
        print(
            "talk2me ready — start talking. Ctrl-C to quit. "
            "Created by @fidgetcoding :)",
            flush=True,
        )
        tools_mode = (
            "auto-approve ⚡"
            if "bypass" in cfg.permission_mode.lower()
            else "gated (spoken approvals)"
        )
        print(
            f"   model: {cfg.model or 'claude default'} · "
            f"ears: {cfg.stt} · voice: {cfg.voice or 'system'} "
            f"@{cfg.rate_wpm or 'default'}wpm · "
            f"barge-in: {'ON' if cfg.barge_in else 'off'} · "
            f"tools: {tools_mode}",
            flush=True,
        )
        print(f"   working on: {cfg.cwd or os.getcwd()}", flush=True)
        if cfg.half_duplex:
            print(
                "   (half-duplex: talking over the agent mid-speech is ignored "
                "— run with --barge-in to interrupt it)",
                flush=True,
            )

    def transcript_path(self, path: str) -> None:
        print(f"📝 saving transcript to {path}", flush=True)

    def speaker_downgrade(self) -> None:
        print(
            "🔈 speakers + --no-aec — I'll mute my ears only while I'm "
            "actually speaking (so I never hear myself). Interrupt me in any "
            "gap; drop --no-aec for full talk-over.",
            flush=True,
        )

    def device_error(self, msg: str) -> None:
        print(
            f"[device] {msg}\n\nRun `talk2me --list-devices` to see options.",
            flush=True,
        )

    def listening(self, *, nl: bool = True) -> None:
        print(f"{chr(10) if nl else ''}🎧 listening…", flush=True)

    def noise_ignored(self) -> None:
        print("   (ignored — transcription noise)", flush=True)

    def paused(self) -> None:
        print("\n⏸  paused — say 'wake up' when you need me", flush=True)

    def still_paused(self) -> None:
        print("⏸  still paused — say 'wake up'", flush=True)

    def paused_ignored(self, text: str) -> None:
        print(f"   (paused — ignored: {text})", flush=True)

    def awake(self) -> None:
        print("\n▶️  awake — listening again", flush=True)

    def waiting_for_rest(self) -> None:
        print("   (…waiting for the rest)", flush=True)

    def noise_resend(self) -> None:
        print("\n   (noise interrupt — repeating your question)", flush=True)

    def you(self, text: str, kind: str = "") -> None:
        label = f"you ({kind})" if kind else "you"
        print(f"\n🗣  {label}: {text}", flush=True)

    def agent_begin(self) -> None:
        self._ta_inline = True
        sys.stdout.write("🤖 ")
        sys.stdout.flush()

    def agent_delta(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def agent_end(self) -> None:
        self._ta_inline = False
        print(flush=True)

    def thinking(self, text: str) -> None:
        # v1 never showed thinking; Plain stays v1. Retro streams it dim.
        pass

    def tool(
        self, name: str, detail: str = "", *, body: str = "", follow_on: bool = False
    ) -> None:
        if follow_on:
            if detail:
                print(f"      ↳ {detail}", flush=True)
            self._body(body)
            return
        suffix = f" — {detail}" if detail else ""
        print(f"\n   [tool] {name}{suffix}", flush=True)
        self._body(body)

    @staticmethod
    def _body(body: str) -> None:
        for line in body.splitlines():
            print(f"      │ {line}", flush=True)

    def working(self, tool_count: int) -> None:
        print(
            f"   ⚙ still working… ({tool_count} tool "
            f"call{'s' if tool_count != 1 else ''} so far)",
            flush=True,
        )

    def barge_label(self, spoke_any: bool) -> None:
        label = "[barge-in] listening…" if spoke_any else "[go on…]"
        print(f"\n   {label}", flush=True)

    def permission_ask(self, tool: str, detail: str) -> None:
        print(f"\n   [permission] {tool}: {detail}", flush=True)

    def permission_heard(self, heard: str, decision: str | None) -> None:
        print(
            f"   [permission] you: {heard} -> {decision or 'unclear'}",
            flush=True,
        )

    def permission_verdict(self, tool: str, allowed: bool) -> None:
        print(
            f"   [permission] {'APPROVED' if allowed else 'DENIED'}: {tool}",
            flush=True,
        )

    def status_note(self, text: str) -> None:
        print(f"   ({text})", flush=True)

    def error(self, msg: str) -> None:
        print(f"\n{msg}", flush=True)

    def debug(self, msg: str, *, nl: bool = False) -> None:
        print(f"{chr(10) if nl else ''}  {msg}", flush=True)

    def close(self) -> None:
        pass


def build_renderer(cfg: Config) -> Renderer:
    """Pick the renderer for a voice session.

    Plain wins on: --plain, a non-TTY stdout (pipes, CI), NO_COLOR, or rich
    failing to import. Anything else gets the retro skin.
    """
    if cfg.plain or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return PlainRenderer()
    try:
        from .retro import RetroRenderer
    except ImportError:
        return PlainRenderer()
    return RetroRenderer()

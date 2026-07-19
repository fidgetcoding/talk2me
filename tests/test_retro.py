"""RetroRenderer smoke + safety suite. No audio, no subprocess, no TTY needed.

Three invariants:
1. Every renderer method runs without raising and emits ANSI color (the skin
   is actually on).
2. Rich markup arriving IN content renders as literal text — a reply saying
   "[red]" must never turn the screen red (injection defense).
3. build_renderer() picks Plain for --plain / NO_COLOR / non-TTY, Retro
   otherwise.

Run:  ./.venv/bin/python -m tests.test_retro
"""

import io
import os
import re

from rich.console import Console

from talk2me.config import Config
from talk2me.render import PlainRenderer, build_renderer
from talk2me.retro import RetroRenderer


def _fresh() -> tuple[RetroRenderer, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=100)
    return RetroRenderer(console=console), buf


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    return ok


def test_smoke_all_methods() -> bool:
    """Every method of the seam runs and the output carries ANSI codes."""
    r, buf = _fresh()
    cfg = Config(
        model="opus", stt="whisper", cwd="/tmp/x", half_duplex=True,
        save_dir="/tmp/logs",
    )
    r.loading_ears()
    r.startup(cfg)
    r.transcript_path("/tmp/t2m.md")
    r.speaker_downgrade()
    r.device_error("no device matches 'Megapods'")
    r.listening()
    r.listening(nl=False)
    r.noise_ignored()
    r.paused()
    r.still_paused()
    r.paused_ignored("chatter")
    r.awake()
    r.waiting_for_rest()
    r.noise_resend()
    r.you("count to ten")
    r.you("and stop", "continued")
    r.you("wait", "barge-in")
    r.agent_begin()
    r.agent_delta("Sure — ")
    r.agent_delta("counting now.")
    r.agent_end()
    r.thinking("let me plan ")
    r.thinking("the file layout")
    r.agent_delta("Here's the plan.")
    r.agent_end()
    r.tool("Write", "pong.html", body="<html>\n<canvas></canvas>\n</html>")
    r.tool("Write", "pong.html", follow_on=True)
    r.tool("Bash")
    r.working(1)
    r.working(9)
    r.barge_label(True)
    r.barge_label(False)
    r.permission_ask("Bash", "command=git status")
    r.permission_heard("yes", "approve")
    r.permission_heard("mumble", None)
    r.permission_verdict("Bash", True)
    r.permission_verdict("Write", False)
    r.error("[backend error] boom")
    r.debug("[t] stt 0.30s")
    r.debug("[t] first-token 1.10s", nl=True)
    r.close()
    out = buf.getvalue()
    # Content checks run on the ANSI-stripped text: the syntax highlighter
    # legitimately inserts color codes BETWEEN tokens of the same line.
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)
    checks = [
        "\x1b[" in out,  # ANSI escapes present — the skin is on
        "listening…" in plain,
        "count to ten" in plain,
        "pong.html" in plain,
        "APPROVED" in plain and "DENIED" in plain,
        "┄" in plain,  # the dotted border made it to the screen
        "@fidgetcoding" in plain,
        "████" in plain,  # the launch banner (width 100 fits it)
        "working on" in plain and "saves to" in plain,  # both path rows
        "🧠" in plain and "the file layout" in plain,  # thinking stream, dim
        "<canvas></canvas>" in plain,  # the actual code, on screen
    ]
    return _report(f"smoke: all methods + ANSI ({sum(checks)}/{len(checks)})", all(checks))


def test_markup_injection_is_literal() -> bool:
    """Hostile rich markup in agent/user/tool content renders as characters.

    Asserted on the ANSI-stripped output: after removing color codes, the
    payload must be present verbatim — proof that neither the markup engine
    nor the auto-highlighter consumed a single character of content.
    """
    ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

    def _tool_detail_via_panel(r, p):
        # tool() opens the Live panel; content only hits the screen on a
        # frame paint. Force one, then fold — both surfaces must stay literal.
        r.tool("Write", p)
        r._live.refresh()
        r.close()

    payloads = [
        ("agent_delta", "[bold red]PWNED[/bold red]",
         lambda r, p: r.agent_delta(p)),
        ("you", "[blink]hi[/blink]", lambda r, p: r.you(p)),
        ("tool detail (panel frame)", "[red]x[/red]", _tool_detail_via_panel),
        ("tool follow-on", "[red]x[/red]",
         lambda r, p: r.tool("Write", p, follow_on=True)),
        ("tool body (code card)", "[red]evil[/red]",
         lambda r, p: r.tool("Write", "x.py", body=p)),
        ("permission ask", "[red]rm -rf /[/red]",
         lambda r, p: r.permission_ask("Bash", p)),
        ("permission heard", "[u]yes[/u]",
         lambda r, p: r.permission_heard(p, "approve")),
        ("error", "[i]oops[/i]", lambda r, p: r.error(p)),
        ("debug", "[s]t[/s]", lambda r, p: r.debug(p)),
        ("paused_ignored", "[red]x[/red]", lambda r, p: r.paused_ignored(p)),
        ("device_error", "[red]dev[/red]", lambda r, p: r.device_error(p)),
    ]
    results = []
    for name, payload, fn in payloads:
        r, buf = _fresh()
        fn(r, payload)
        stripped = ansi.sub("", buf.getvalue())
        ok = payload in stripped
        if not ok:
            print(f"  INJECTION LEAK via {name}: {stripped!r}")
        results.append(ok)
    return _report(
        f"markup injection stays literal ({sum(results)}/{len(results)})",
        all(results),
    )


def test_live_work_panel() -> bool:
    """The Phase-4 interleave discipline: tools open ONE Live region; any
    prose/status/permission output collapses it to a summary line FIRST."""
    ansi = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    results = []

    # tool() opens the panel; agent_delta collapses it before prose lands.
    r, buf = _fresh()
    r.agent_begin()
    r.tool("Write")
    results.append(r._live is not None)  # panel is up
    r.tool("Write", "pong.html", follow_on=True)
    results.append(r._panel is not None and r._panel.entries[-1] == ("Write", "pong.html"))
    r.tool("Bash", "open pong.html")
    results.append(r._panel is not None and r._panel.count == 2)
    before = len(buf.getvalue())
    r.working(2)
    results.append(len(buf.getvalue()) == before)  # panel live -> working() is silent
    r.agent_delta("all done here")
    results.append(r._live is None)  # collapsed before prose
    out = buf.getvalue()
    stripped = ansi.sub("", out)
    results.append("⚙ 2 tool calls · " in stripped)  # the permanent summary line
    # The summary (last "tool call" emission) precedes the prose.
    results.append(out.rindex("tool call") < out.rindex("all done here"))

    # Ctrl-C path: close() with a live panel restores and never raises.
    r2, buf2 = _fresh()
    r2.tool("Bash", "make test")
    results.append(r2._live is not None)
    r2.close()
    results.append(r2._live is None)
    results.append("⚙ 1 tool call · " in ansi.sub("", buf2.getvalue()))
    r2.close()  # second close is a no-op, not a crash
    results.append(True)

    # Permission gate collapses the panel before asking.
    r3, buf3 = _fresh()
    r3.tool("Write")
    r3.permission_ask("Write", "file_path=x.txt")
    results.append(r3._live is None)
    results.append("[permission]" in ansi.sub("", buf3.getvalue()))

    # After a collapse, a late follow-on detail prints linear (Plain-style).
    r4, buf4 = _fresh()
    r4.tool("Write")
    r4.agent_delta("text")
    r4.tool("Write", "late.txt", follow_on=True)
    results.append("↳ late.txt" in ansi.sub("", buf4.getvalue()))

    # working() prints its fallback line when no panel is live.
    r5, buf5 = _fresh()
    r5.working(3)
    results.append("still working… (3 tool calls so far)" in ansi.sub("", buf5.getvalue()))

    return _report(f"live work panel ({sum(results)}/{len(results)})", all(results))


def test_renderer_selection() -> bool:
    results = []
    had = os.environ.pop("NO_COLOR", None)
    try:
        # Non-TTY (this test process) -> Plain even without --plain.
        results.append(isinstance(build_renderer(Config()), PlainRenderer))
        # --plain -> Plain, everywhere, always.
        results.append(isinstance(build_renderer(Config(plain=True)), PlainRenderer))
        # NO_COLOR -> Plain.
        os.environ["NO_COLOR"] = "1"
        results.append(isinstance(build_renderer(Config()), PlainRenderer))
    finally:
        if had is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = had
    return _report(f"renderer selection ({sum(results)}/{len(results)})", all(results))


def main() -> int:
    results = [
        test_smoke_all_methods(),
        test_markup_injection_is_literal(),
        test_live_work_panel(),
        test_renderer_selection(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall "
          f"({sum(results)}/{len(results)} groups passed)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Byte-parity snapshots for PlainRenderer.

Every expected string below was copied verbatim from the launch build's print
sites (orchestrator.py / __main__.py at v1.0.0) BEFORE the renderer sweep —
these snapshots are the proof that moving the prints behind the seam changed
nothing. If one of these fails, the seam broke the product's face; fix the
renderer, never the snapshot (unless the change is deliberate and documented).

Run:  ./.venv/bin/python -m tests.test_render
"""

import contextlib
import io
import os

from talk2me.config import Config
from talk2me.render import PlainRenderer


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    return ok


def _check(results: list, name: str, got: str, want: str) -> None:
    ok = got == want
    if not ok:
        print(f"  MISMATCH {name}:\n    got  {got!r}\n    want {want!r}")
    results.append(ok)


def test_startup_lines() -> bool:
    r = PlainRenderer()
    results: list = []
    _check(results, "loading_ears", _capture(r.loading_ears), "(loading the ears…)\n")

    cfg = Config(
        model="opus",
        stt="whisper",
        voice=None,
        rate_wpm=236,
        barge_in=False,
        half_duplex=True,
        permission_mode="bypassPermissions",
        cwd="/tmp/proj",
    )
    _check(
        results,
        "startup (half-duplex, bypass)",
        _capture(r.startup, cfg),
        "talk2me ready — start talking. Ctrl-C to quit. "
        "Created by @fidgetcoding :)\n"
        "   model: opus · ears: whisper · voice: system @236wpm · "
        "barge-in: off · tools: auto-approve ⚡\n"
        "   working on: /tmp/proj\n"
        "   (half-duplex: talking over the agent mid-speech is ignored "
        "— run with --barge-in and headphones to interrupt it)\n",
    )

    cfg2 = Config(
        model=None,
        stt="parakeet",
        voice="Ava (Premium)",
        rate_wpm=236,
        barge_in=True,
        half_duplex=False,
        permission_mode="default",
        cwd="/tmp/proj",
    )
    _check(
        results,
        "startup (barge-in, gated, no hint)",
        _capture(r.startup, cfg2),
        "talk2me ready — start talking. Ctrl-C to quit. "
        "Created by @fidgetcoding :)\n"
        "   model: claude default · ears: parakeet · "
        "voice: Ava (Premium) @236wpm · "
        "barge-in: ON · tools: gated (spoken approvals)\n"
        "   working on: /tmp/proj\n",
    )

    # cwd=None falls back to the process cwd, exactly like the old print.
    cfg3 = Config(cwd=None, half_duplex=False)
    got = _capture(r.startup, cfg3)
    results.append(f"   working on: {os.getcwd()}\n" in got)

    _check(
        results,
        "transcript_path",
        _capture(r.transcript_path, "/tmp/t2m-x.md"),
        "📝 saving transcript to /tmp/t2m-x.md\n",
    )
    _check(
        results,
        "speaker_downgrade",
        _capture(r.speaker_downgrade),
        "🔈 speakers on the output — I'll mute my ears only while I'm "
        "actually speaking (so I never hear myself). Interrupt me in any "
        "gap; headphones add talk-over.\n",
    )
    _check(
        results,
        "device_error",
        _capture(r.device_error, "no input device matches 'Megapods'"),
        "[device] no input device matches 'Megapods'\n\n"
        "Run `talk2me --list-devices` to see options.\n",
    )
    return _report(f"startup lines ({sum(results)}/{len(results)})", all(results))


def test_loop_lines() -> bool:
    r = PlainRenderer()
    results: list = []
    _check(results, "listening", _capture(r.listening), "\n🎧 listening…\n")
    _check(
        results, "listening no-nl", _capture(lambda: r.listening(nl=False)),
        "🎧 listening…\n",
    )
    _check(
        results, "noise_ignored", _capture(r.noise_ignored),
        "   (ignored — transcription noise)\n",
    )
    _check(
        results, "paused", _capture(r.paused),
        "\n⏸  paused — say 'wake up' when you need me\n",
    )
    _check(
        results, "still_paused", _capture(r.still_paused),
        "⏸  still paused — say 'wake up'\n",
    )
    _check(
        results, "paused_ignored", _capture(r.paused_ignored, "some words"),
        "   (paused — ignored: some words)\n",
    )
    _check(results, "awake", _capture(r.awake), "\n▶️  awake — listening again\n")
    _check(
        results, "waiting_for_rest", _capture(r.waiting_for_rest),
        "   (…waiting for the rest)\n",
    )
    _check(
        results, "noise_resend", _capture(r.noise_resend),
        "\n   (noise interrupt — repeating your question)\n",
    )
    _check(results, "you", _capture(r.you, "count to ten"), "\n🗣  you: count to ten\n")
    _check(
        results, "you continued", _capture(r.you, "and then stop", "continued"),
        "\n🗣  you (continued): and then stop\n",
    )
    _check(
        results, "you barge-in", _capture(r.you, "stop", "barge-in"),
        "\n🗣  you (barge-in): stop\n",
    )
    _check(results, "agent_begin", _capture(r.agent_begin), "🤖 ")
    _check(results, "agent_delta", _capture(r.agent_delta, "Sure — on it."), "Sure — on it.")
    _check(results, "agent_end", _capture(r.agent_end), "\n")
    _check(
        results, "barge_label spoke", _capture(r.barge_label, True),
        "\n   [barge-in] listening…\n",
    )
    _check(
        results, "barge_label silent", _capture(r.barge_label, False),
        "\n   [go on…]\n",
    )
    _check(
        results, "debug", _capture(r.debug, "[t] stt 0.32s"),
        "  [t] stt 0.32s\n",
    )
    _check(
        results, "debug nl", _capture(lambda: r.debug("[t] first-token 1.20s", nl=True)),
        "\n  [t] first-token 1.20s\n",
    )
    _check(
        results, "error", _capture(r.error, "[backend error] claude exited rc=1"),
        "\n[backend error] claude exited rc=1\n",
    )
    _check(results, "close is silent", _capture(r.close), "")
    return _report(f"loop lines ({sum(results)}/{len(results)})", all(results))


def test_tool_and_permission_lines() -> bool:
    r = PlainRenderer()
    results: list = []
    _check(results, "tool bare", _capture(r.tool, "Write"), "\n   [tool] Write\n")
    _check(
        results, "tool with detail", _capture(r.tool, "Write", "pong.html"),
        "\n   [tool] Write — pong.html\n",
    )
    _check(
        results, "tool follow-on",
        _capture(lambda: r.tool("Write", "pong.html", follow_on=True)),
        "      ↳ pong.html\n",
    )
    _check(
        results, "tool follow-on empty",
        _capture(lambda: r.tool("Write", "", follow_on=True)),
        "",
    )
    _check(
        results, "tool with body",
        _capture(lambda: r.tool("Write", "pong.html", body="<html>\n</html>")),
        "\n   [tool] Write — pong.html\n      │ <html>\n      │ </html>\n",
    )
    _check(
        results, "tool follow-on with body",
        _capture(lambda: r.tool("Bash", "", body="echo a\necho b", follow_on=True)),
        "      │ echo a\n      │ echo b\n",
    )
    _check(results, "thinking is silent in Plain", _capture(r.thinking, "hmm"), "")
    _check(
        results, "status_note",
        _capture(r.status_note, "resuming the interrupted task"),
        "   (resuming the interrupted task)\n",
    )
    _check(
        results, "working singular", _capture(r.working, 1),
        "   ⚙ still working… (1 tool call so far)\n",
    )
    _check(
        results, "working plural", _capture(r.working, 9),
        "   ⚙ still working… (9 tool calls so far)\n",
    )
    _check(
        results, "permission_ask",
        _capture(r.permission_ask, "Bash", "command=git status"),
        "\n   [permission] Bash: command=git status\n",
    )
    _check(
        results, "permission_heard",
        _capture(r.permission_heard, "yes go ahead", "approve"),
        "   [permission] you: yes go ahead -> approve\n",
    )
    _check(
        results, "permission_heard unclear",
        _capture(r.permission_heard, "hmm what", None),
        "   [permission] you: hmm what -> unclear\n",
    )
    _check(
        results, "permission approved", _capture(r.permission_verdict, "Bash", True),
        "   [permission] APPROVED: Bash\n",
    )
    _check(
        results, "permission denied", _capture(r.permission_verdict, "Write", False),
        "   [permission] DENIED: Write\n",
    )
    return _report(
        f"tool + permission lines ({sum(results)}/{len(results)})", all(results)
    )


def main() -> int:
    results = [
        test_startup_lines(),
        test_loop_lines(),
        test_tool_and_permission_lines(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall "
          f"({sum(results)}/{len(results)} groups passed)")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

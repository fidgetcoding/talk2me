"""The input line (typeline.py), headless.

LineEditor is a pure byte-stream state machine — every editing rule the
terminal used to own is locked here: submits, backspace, Ctrl-U, escape
sequences (arrows must never inject '[A' into the buffer), UTF-8 split
across reads, paste floods with embedded newlines. Then one real-PTY
round-trip: cbreak on, keystrokes in, submit out, termios restored.

Run:  ./.venv/bin/python -m tests.test_typeline
"""

import asyncio
import os
import pty
import termios

from talk2me.typeline import LineEditor, TypeLine

FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS += 1


def test_editor() -> None:
    ed = LineEditor()
    check("plain chars", ed.feed(b"hello") == [] and ed.buffer == "hello")
    check("enter submits", ed.feed(b"\r") == ["hello"] and ed.buffer == "")

    ed.feed(b"heidk")
    ed.feed(b"\x7f\x7f\x7f")
    check("backspace edits", ed.buffer == "he")
    ed.feed(b"\x15")
    check("ctrl-u wipes", ed.buffer == "")

    ed.feed(b"up\x1b[A\x1b[Bdown")  # arrow keys mid-typing
    check("CSI sequences swallowed", ed.buffer == "updown", repr(ed.buffer))
    ed.feed(b"\x1bOC")  # SS3 arrow form
    check("SS3 sequences swallowed", ed.buffer == "updown")
    ed.feed(b"\x15")

    # UTF-8 split across reads (é = 0xc3 0xa9).
    ed.feed(b"caf\xc3")
    ed.feed(b"\xa9")
    check("utf-8 split across reads", ed.buffer == "café", repr(ed.buffer))
    ed.feed(b"\x15")

    # Paste flood with embedded newlines = one submit per line, remainder
    # stays buffered (the 80ms assembler downstream joins the burst).
    subs = ed.feed(b"line one\nline two\nline thr")
    check(
        "paste flood submits per line",
        subs == ["line one", "line two"] and ed.buffer == "line thr",
        f"{subs} + {ed.buffer!r}",
    )
    ed.feed(b"\x15")

    # Control chars that must be inert (Ctrl-D, tab).
    ed.feed(b"ok\x04\x09ok")
    check("stray control chars inert", ed.buffer == "okok", repr(ed.buffer))


async def test_pty_roundtrip() -> None:
    master, slave = pty.openpty()
    loop = asyncio.get_running_loop()
    submits: list[str] = []
    changes: list[str] = []
    before = termios.tcgetattr(slave)

    tl = TypeLine(
        loop,
        on_submit=submits.append,
        on_change=changes.append,
        fd=slave,
    )
    check("available() on a pty", TypeLine.available(slave))
    tl.start()
    cbreak = termios.tcgetattr(slave)
    check(
        "cbreak armed (echo off)",
        not (cbreak[3] & termios.ECHO) and not (cbreak[3] & termios.ICANON),
    )
    check("ISIG stays on (Ctrl-C works)", bool(cbreak[3] & termios.ISIG))

    os.write(master, b"ship it")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if changes and changes[-1] == "ship it":
            break
    check("keystrokes reach on_change", changes and changes[-1] == "ship it")

    os.write(master, b"\r")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if submits:
            break
    check("enter reaches on_submit", submits == ["ship it"], str(submits))

    tl.close()
    after = termios.tcgetattr(slave)
    check(
        "close() restores termios",
        bool(after[3] & termios.ECHO) == bool(before[3] & termios.ECHO),
    )
    os.close(master)
    os.close(slave)


async def main() -> int:
    test_editor()
    await test_pty_roundtrip()
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILURES'}")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

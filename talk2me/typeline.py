"""The input line — typing at talk2me without wrecking the screen.

The v2.2-v2.4 typed path left the terminal in canonical mode: every
keystroke echoed wherever the cursor happened to be, which during streamed
prose produced live messes like "not sure keep 🤖 Gotyou it, stopped". This
module takes the terminal into cbreak (no echo, no line buffering — but
Ctrl-C/Ctrl-T still work, ISIG stays on) and owns the line editing itself:

- printable keys build a buffer the RENDERER displays (a ⌨ row inside the
  work panel while tools run, a bottom line while idle, silence mid-prose);
- Enter submits the buffer into the same stdin-lines queue the old readline
  pump fed, so the 80ms paste assembler and everything downstream (typed
  turns, takeover, controls) is untouched;
- backspace/Ctrl-U edit, escape sequences (arrow keys) are swallowed whole,
  UTF-8 survives being split across reads.

Only arms on a real TTY; pipes/CI/tests keep the legacy readline pump. The
saved termios state is restored on close() AND via a module registry the
hard-exit path drains first (os._exit skips atexit — a session must never
leave the shell echo-less).
"""

from __future__ import annotations

import codecs
import os
import sys
import termios
import threading
import tty

# fd -> saved termios attrs, drained by restore_all() on any exit path.
_SAVED: dict[int, list] = {}
_SAVED_LOCK = threading.Lock()


def restore_all() -> None:
    """Put every cbreak'd fd back. Safe to call twice, from any thread —
    the hard-exit path (os._exit after 'bye.') calls this first."""
    with _SAVED_LOCK:
        items = list(_SAVED.items())
        _SAVED.clear()
    for fd, attrs in items:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass


class LineEditor:
    """Pure byte-stream -> (buffer, submits) state machine. No terminal,
    no threads — this is the tested core. Feed it raw bytes; it maintains
    the visible buffer and returns completed lines."""

    def __init__(self) -> None:
        self.buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Escape-sequence swallow state: "" | "esc" | "csi"
        self._esc = ""

    def feed(self, data: bytes) -> list[str]:
        """Consume raw bytes; return the list of submitted lines (usually
        empty or one). self.buffer reflects the in-progress line after."""
        submits: list[str] = []
        for ch in self._decoder.decode(data):
            if self._esc == "esc":
                # ESC [ opens a CSI sequence; ESC O is the SS3 arrow form;
                # anything else ends the escape immediately.
                self._esc = "csi" if ch in "[O" else ""
                continue
            if self._esc == "csi":
                # CSI ends on a final byte @ through ~ (covers arrows,
                # home/end, delete, bracketed-paste markers).
                if "@" <= ch <= "~":
                    self._esc = ""
                continue
            if ch == "\x1b":
                self._esc = "esc"
                continue
            if ch in ("\r", "\n"):
                submits.append(self.buffer)
                self.buffer = ""
                continue
            if ch in ("\x7f", "\x08"):
                self.buffer = self.buffer[:-1]
                continue
            if ch == "\x15":  # Ctrl-U — wipe the line
                self.buffer = ""
                continue
            if ch < " " or ch == "\x04":
                # Other control chars (Ctrl-D, tab for now) — ignore.
                # Ctrl-C/Ctrl-T never arrive here: ISIG is left on.
                continue
            self.buffer += ch
        return submits


class TypeLine:
    """The armed input line: cbreak terminal + reader thread + LineEditor.

    on_submit(text) and on_change(buffer) fire on the event loop via
    call_soon_threadsafe. Renderer display is the caller's business —
    this class only owns the terminal state and the keystream.
    """

    def __init__(self, loop, on_submit, on_change, fd: int | None = None) -> None:
        self._loop = loop
        self._on_submit = on_submit
        self._on_change = on_change
        self._fd = sys.stdin.fileno() if fd is None else fd
        self._editor = LineEditor()
        self._closed = False

    @staticmethod
    def available(fd: int | None = None) -> bool:
        try:
            f = sys.stdin.fileno() if fd is None else fd
            return os.isatty(f)
        except Exception:
            return False

    def start(self) -> None:
        attrs = termios.tcgetattr(self._fd)
        with _SAVED_LOCK:
            _SAVED[self._fd] = attrs
        tty.setcbreak(self._fd)
        threading.Thread(
            target=self._reader, daemon=True, name="t2m-typeline"
        ).start()

    def _reader(self) -> None:
        while not self._closed:
            try:
                data = os.read(self._fd, 512)
            except OSError:
                return
            if not data:
                return
            submits = self._editor.feed(data)
            buf = self._editor.buffer
            try:
                for line in submits:
                    self._loop.call_soon_threadsafe(self._on_submit, line)
                self._loop.call_soon_threadsafe(self._on_change, buf)
            except RuntimeError:
                return  # loop is closing — session teardown

    def close(self) -> None:
        self._closed = True
        with _SAVED_LOCK:
            attrs = _SAVED.pop(self._fd, None)
        if attrs is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, attrs)
            except Exception:
                pass

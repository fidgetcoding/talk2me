"""Wispr-hands-free spike — synthesize a key combo via Quartz CGEvent.

KNOWN-RISK SPIKE. Not wired into the live CLI. Goal: have talk2me synthesize
Wispr Flow's activation hotkey so it can ARM Wispr before listening and DISARM
it during TTS, making the dictation loop hands-free.

How it works: macOS exposes synthetic input through Quartz Event Services. We
build a key-down + key-up `CGEvent` for the chosen key, stamp it with the
modifier flag mask (ctrl / option / cmd / shift), and post it to the HID event
tap. Whatever app is frontmost receives it as if the user pressed the keys.

Hard caveats (see docs/wispr-spike.md for the full procedure):
  * Fn / Globe is NOT reliably synthesizable via CGEvent — the plan is to
    rebind Wispr's trigger to a normal combo (e.g. ctrl+opt+space).
  * Posting synthetic keys requires Accessibility permission for the host
    process (Terminal / python) in System Settings → Privacy & Security.
  * Full verification needs the desktop + Wispr running. This module is safe to
    *import* anywhere; Quartz is lazy-imported only when you actually tap.

pyobjc-framework-Quartz is intentionally NOT a project dependency — this is a
spike. Install it manually:  pip install pyobjc-framework-Quartz
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # keep the module import-clean without Quartz installed
    pass

# --- Static maps (no Quartz import needed to read these) ----------------------

# Modifier name -> CGEventFlags mask bit. Aliases included ("alt" == "option").
# Values are the documented kCGEventFlagMask* constants from Quartz CoreGraphics.
MODIFIERS: dict[str, int] = {
    "shift": 0x00020000,    # kCGEventFlagMaskShift
    "control": 0x00040000,  # kCGEventFlagMaskControl
    "ctrl": 0x00040000,
    "option": 0x00080000,   # kCGEventFlagMaskAlternate
    "alt": 0x00080000,
    "opt": 0x00080000,
    "command": 0x00100000,  # kCGEventFlagMaskCommand
    "cmd": 0x00100000,
}

# Key name -> macOS virtual keycode (kVK_*, from HIToolbox/Events.h). These are
# layout-independent hardware codes, stable across macOS versions.
KEYCODES: dict[str, int] = {
    # letters (kVK_ANSI_*)
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
    # digits (kVK_ANSI_*)
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22,
    "7": 26, "8": 28, "9": 25, "0": 29,
    # common named keys
    "space": 49,    # kVK_Space
    "return": 36,   # kVK_Return
    "enter": 36,
    "tab": 48,      # kVK_Tab
    "escape": 53,   # kVK_Escape
    "esc": 53,
    "delete": 51,   # kVK_Delete (backspace)
    "backspace": 51,
    # function keys (kVK_F1.. ) — handy alternate Wispr triggers
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_QUARTZ_INSTALL_HINT = (
    "pyobjc-framework-Quartz is required to synthesize key events but is not "
    "installed. This is an optional spike dependency (intentionally NOT in "
    "pyproject). Install it with:\n\n    pip install pyobjc-framework-Quartz\n"
)


def _load_quartz() -> Any:
    """Lazy-import Quartz. Raises a clear RuntimeError if pyobjc is missing.

    Kept out of module scope so `import talk2me.input.wispr_spike` works on any
    machine (CI, Linux, a Mac without pyobjc) — only an actual tap needs Quartz.
    """
    try:
        import Quartz  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(_QUARTZ_INSTALL_HINT) from exc
    return Quartz


def resolve_flags(modifiers: list[str]) -> int:
    """OR together the CGEventFlags mask for the given modifier names.

    Raises KeyError-style ValueError with the offending name so a typo in a
    config combo fails loudly rather than silently dropping a modifier.
    """
    mask = 0
    for name in modifiers:
        key = name.strip().lower()
        if key not in MODIFIERS:
            raise ValueError(
                f"unknown modifier {name!r}; known: {sorted(set(MODIFIERS))}"
            )
        mask |= MODIFIERS[key]
    return mask


def resolve_keycode(key: str) -> int:
    """Map a key name to its macOS virtual keycode."""
    name = key.strip().lower()
    if name not in KEYCODES:
        raise ValueError(
            f"unknown key {key!r}; known: {sorted(KEYCODES)}"
        )
    return KEYCODES[name]


def tap_combo(modifiers: list[str], key: str, hold_seconds: float = 0.04) -> None:
    """Synthesize a modifier+key chord: press everything down, then release.

    Posts a key-down then a key-up CGEvent for `key`, both stamped with the
    combined modifier flag mask. macOS delivers it to the frontmost app.

    Args:
        modifiers: e.g. ["ctrl", "opt"] — names from MODIFIERS (aliases OK).
        key: a single key name from KEYCODES (e.g. "space", "a").
        hold_seconds: dwell between down and up so the receiving app registers
            the chord. Wispr's hotkey watcher needs a beat; 40ms is generous.

    Raises:
        RuntimeError: if pyobjc-framework-Quartz isn't installed.
        ValueError: on an unknown modifier or key name.

    Note: requires Accessibility permission for the host process. Without it,
    macOS silently drops the synthetic event (no exception) — see the docs.
    """
    quartz = _load_quartz()
    flags = resolve_flags(modifiers)
    keycode = resolve_keycode(key)

    # Build key-down and key-up. The source (None) uses the combined session.
    down = quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    up = quartz.CGEventCreateKeyboardEvent(None, keycode, False)

    # Stamp modifier flags on both events so the chord reads as held.
    quartz.CGEventSetFlags(down, flags)
    quartz.CGEventSetFlags(up, flags)

    # kCGHIDEventTap (value 0) posts at the lowest tap — closest to real HID.
    tap = quartz.kCGHIDEventTap
    quartz.CGEventPost(tap, down)
    if hold_seconds > 0:
        time.sleep(hold_seconds)
    quartz.CGEventPost(tap, up)


def _describe(modifiers: list[str], key: str) -> str:
    parts = [m.strip().lower() for m in modifiers] + [key.strip().lower()]
    return "+".join(parts)


def main(argv: list[str] | None = None) -> int:
    """Spike entry point: countdown, then tap a combo at the frontmost app.

    Usage:
        python -m talk2me.input.wispr_spike [modifiers...] <key>

    Examples:
        python -m talk2me.input.wispr_spike ctrl opt space
        python -m talk2me.input.wispr_spike cmd shift a
        python -m talk2me.input.wispr_spike            # defaults to ctrl+opt+space

    The 3-second countdown lets you click into a TextEdit window (or focus
    Wispr's target) before the synthetic keys fire.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        modifiers, key = ["ctrl", "opt"], "space"
    else:
        *modifiers, key = args

    combo = _describe(modifiers, key)
    print(f"[wispr-spike] will send: {combo}")

    # Validate the combo BEFORE the countdown so typos fail fast.
    try:
        resolve_flags(modifiers)
        resolve_keycode(key)
    except ValueError as exc:
        print(f"[wispr-spike] ERROR: {exc}", file=sys.stderr)
        return 2

    print("[wispr-spike] focus your target window now...")
    for remaining in (3, 2, 1):
        print(f"[wispr-spike] firing in {remaining}...", flush=True)
        time.sleep(1)

    try:
        tap_combo(modifiers, key)
    except RuntimeError as exc:
        print(f"[wispr-spike] ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        f"[wispr-spike] sent {combo}. "
        "If nothing happened: grant Accessibility to this process "
        "(System Settings → Privacy & Security → Accessibility) and retry."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual spike entry
    raise SystemExit(main())

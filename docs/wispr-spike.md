# Wispr-hands-free spike

**Status: KNOWN-RISK SPIKE. Not wired into the CLI.** This documents how to
manually verify that talk2me can synthesize Wispr Flow's activation hotkey so
the voice loop can ARM Wispr before listening and DISARM it during TTS — making
dictation fully hands-free.

The code lives in [`talk2me/input/wispr_spike.py`](../talk2me/input/wispr_spike.py).

## The idea

Wispr Flow is a dictation app with great transcription + a custom dictionary.
We don't transcribe inside talk2me when Wispr is driving — instead talk2me
*presses Wispr's hotkey for you*:

- **ARM** — tap the combo right before talk2me starts listening, so Wispr
  begins dictating into the focused field.
- **DISARM** — tap it again (or its stop combo) when talk2me starts TTS, so
  Wispr isn't capturing the speaker's own audio.

The spike proves the first half: can we synthesize a key combo that Wispr
actually reacts to?

## Why a normal combo, not Fn/Globe

Wispr's default trigger is often **Fn (Globe)**. Fn is **not reliably
synthesizable** via Quartz `CGEvent` — it's handled at a layer below the normal
HID keycode path on Apple keyboards, so a synthetic Fn press is frequently
dropped. **The plan is to rebind Wispr's trigger to a normal chord** such as
`ctrl + opt + space`, which `CGEventCreateKeyboardEvent` handles cleanly.

## One-time setup

### 1. Install the spike dependency (NOT a project dep)

`pyobjc-framework-Quartz` is intentionally **not** in `pyproject.toml` — this is
a spike. Install it into the project venv only when testing:

```bash
/Users/nathandavidovich/code/talk2me/.venv/bin/pip install pyobjc-framework-Quartz
```

The module imports fine without it; only an actual key tap needs it (you'll get
a clear `RuntimeError` with this same install hint if it's missing).

### 2. Rebind Wispr's trigger

In Wispr Flow's settings, change the activation shortcut from Fn/Globe to a
normal combo. Default the spike assumes **`ctrl + opt + space`**. Pick anything
that doesn't collide with a system or app shortcut.

### 3. Grant Accessibility permission

Synthesizing input requires Accessibility. Go to **System Settings → Privacy &
Security → Accessibility** and enable the process that runs Python — usually
your terminal app (**Terminal**, **Ghostty**, **iTerm2**, …). If you run the
venv's python binary directly, you may need to add that binary too.

> Without this permission macOS **silently drops** synthetic events — no error,
> nothing happens. If the tap "succeeds" but Wispr doesn't arm, this is the
> first thing to check.

## Manual verification procedure

Full headless verification is **impossible** — it needs the live desktop +
Wispr running + Accessibility granted. Run these by hand:

### Smoke test (no Wispr, proves keys land)

1. Open **TextEdit** and create a blank document. Click into it so it's focused.
2. Run the spike, sending a printable key:

   ```bash
   /Users/nathandavidovich/code/talk2me/.venv/bin/python \
     -m talk2me.input.wispr_spike a
   ```

3. There's a 3-second countdown — make sure TextEdit is focused before it fires.
4. Confirm the letter `a` appears in TextEdit. If it does, synthetic keys are
   landing and Accessibility is correctly granted.

### Wispr arm test (the real check)

1. Confirm Wispr's trigger is rebound to your chosen combo (e.g. `ctrl+opt+space`).
2. Focus a TextEdit window.
3. Fire the rebound combo:

   ```bash
   /Users/nathandavidovich/code/talk2me/.venv/bin/python \
     -m talk2me.input.wispr_spike ctrl opt space
   ```

4. After the countdown, watch for Wispr's "listening" indicator. Speak — your
   words should dictate into TextEdit. If Wispr arms, the spike is **validated**.

### Other combos

```bash
# default is ctrl+opt+space when no args are given
python -m talk2me.input.wispr_spike
# cmd+shift+a
python -m talk2me.input.wispr_spike cmd shift a
```

Supported modifiers: `ctrl`/`control`, `opt`/`alt`/`option`, `cmd`/`command`,
`shift`. Supported keys: `space`, `return`/`enter`, `tab`, `esc`, `delete`,
`a`–`z`, `0`–`9`, `f1`–`f12`. See `KEYCODES` / `MODIFIERS` in the module for the
full list.

## What to do if it doesn't work

| Symptom | Likely cause | Fix |
|---|---|---|
| Nothing happens, no error | Accessibility not granted | Enable the terminal/python in Privacy & Security → Accessibility |
| `RuntimeError` about Quartz | pyobjc not installed | `pip install pyobjc-framework-Quartz` into the venv |
| Smoke test types but Wispr won't arm | Wispr still on Fn, or Wispr ignores synthetic keys | Rebind off Fn; if still ignored, see fallback |
| `ValueError: unknown modifier/key` | Typo in the combo args | Use a name from the supported lists above |

## Fallback: manual push-to-talk

If Wispr refuses to react to synthetic keys at all (some apps filter events
where `kCGEventSourceStateID` marks them synthetic), the hands-free Wispr path
is off the table. Fall back to the project's default loop:
**VAD + local whisper** for hands-free, or a **manual push-to-talk** key the
user presses to start/stop a turn. That's the documented default in the project
plan — Wispr-hands-free is a nice-to-have on top, not a dependency.

## Notes

- The module is **safe to import on any machine** (Linux/CI included) — Quartz
  is lazy-imported only inside `tap_combo`.
- Everything past "grant Accessibility" **requires manual testing on the Mac
  desktop** with Wispr installed. There is no automated assertion for "Wispr
  armed" — confirm it by eye.

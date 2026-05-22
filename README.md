# talk2me 🎙️

**Talk to your terminal. It talks back. Then it shuts up and listens again — no buttons, no holding a key like it's a walkie-talkie.**

You say something. It hears you, types it to your coding agent, reads the answer out loud, and reopens the mic on its own. That's the whole loop. You never touch the keyboard unless you want to.

---

## Quick nav

- [Why I built this](#why-i-built-this)
- [What it actually does](#what-it-actually-does)
- [The trick](#the-trick)
- [Quickstart](#quickstart)
- [Ways to run it](#ways-to-run-it)
- [Tuning the ears](#tuning-the-ears)
- [Under the hood](#under-the-hood)
- [Not done yet](#not-done-yet)
- [Requirements](#requirements)
- [Tests](#tests)
- [FAQ](#faq)

---

## Why I built this

I live in Claude Code. All day, in the terminal, sometimes from my phone on the couch over SSH.

The slow part was never the thinking. It was the typing. Long prompts, one finger at a time, while a perfectly good idea evaporated.

Every voice tool I tried made me hold a button for every single turn. Push to talk, let go, wait, push again. That's not hands-free. That's a 1994 CB radio with extra steps.

So I wanted one thing: talk, get an answer out loud, and have it start listening again by itself. Nobody had built that for a terminal coding agent without scraping the screen like a raccoon going through a dumpster. So here we are.

## What it actually does

```
you talk  →  it finds where your sentence ends  →  transcribes it
          →  hands it to Claude Code  →  speaks the reply out loud
          →  reopens the mic  →  you talk again
```

It runs **half-duplex** by default, which is a fancy way of saying it mutes its own ears while it's talking, so it doesn't hear itself and lose its mind. No headphones required. No echo. No feedback spiral.

When it's your turn, it says so (`🎧 listening…`). When you're mid-thought, it waits. When you stop, it goes.

## The trick

Most voice wrappers read your terminal screen and try to guess what the agent said. That's brittle and it means they'll happily read every menu, spinner, and file path out loud.

talk2me doesn't do that. It runs Claude Code in a structured streaming mode and owns the pipe directly — it feeds your words in and reads clean events back out. So it only ever speaks the **actual answer**. The tool calls, the file diffs, the machinery — all of that shows up on screen but never gets read aloud. Your assistant sounds like a person, not a malfunctioning GPS narrating every turn.

That structured connection is the entire reason this works. It's the spine. Everything else is swappable parts hanging off it.

## Quickstart

```bash
pip install -e .

talk2me --model haiku
```

Start talking. Hit `Ctrl-C` when you're done.

## Ways to run it

| Command | What you get |
|---|---|
| `talk2me` | Just talk. The main event. |
| `talk2me --text` | Type instead of talk — for when you're in public and not ready to be the person speaking to their laptop. |
| `talk2me --debug` | Shows what the ears are doing (heard speech, turn ended, too short). Use this the first time. |
| `talk2me --vocab Lorecraft --vocab Morgen` | Teach it your weird proper nouns so it stops inventing spellings. |
| `talk2me --tts null` | Answers on screen, no voice out. |

## Tuning the ears

The default mic settings assume a quiet room and a close mic. If it keeps cutting you off, or won't trigger at all, turn two knobs:

```bash
talk2me --debug --energy-threshold 0.02   # higher = needs you louder (kills background noise)
talk2me --debug --silence-ms 1200          # higher = waits longer before deciding you're done
```

Run with `--debug` while you tune. You'll see exactly when it thinks you started and stopped, so you're adjusting on evidence instead of vibes.

## Under the hood

Every swappable part hides behind a contract: voice detection, transcription, the voice it speaks in, and the agent on the other end. Want a better transcriber or a different voice? That's one new file, not a heart transplant. The main loop never even knows it changed.

Out of the box:

- **Ears (voice detection):** a simple loudness check. Good enough for a quiet room.
- **Transcription:** Whisper, running on your machine. No cloud, no per-minute meter.
- **Voice:** the built-in macOS voice.
- **The agent:** Claude Code.

## Not done yet

Being honest about the edges, because shipping half-true READMEs is how trust dies:

- **Cutting it off mid-sentence** (barge-in). Right now it finishes its thought before it listens again. Interrupting it is built but not wired on by default — it needs echo handling so it doesn't hear itself.
- **Tool approvals by voice.** If the agent wants to run something that needs a yes, there's no way to say yes out loud yet. For now, keep early sessions conversational.
- **Wispr hands-free.** I want to drive the dictation app I actually like instead of the basics. It's a real maybe — the keypress trick it needs isn't proven yet.
- **Fancier ears and a better voice.** Hooks are in. Nobody's filled the slots.

## Requirements

- **macOS** — the voice uses the built-in `say`. Other platforms can plug in their own; nobody has yet.
- **`claude`** on your PATH.
- **Python 3.11+**
- **A microphone**, ideally close to your face.

## Tests

Plain scripts. No frameworks to install. The whole loop is tested without a mic and without spending a cent on the agent:

```bash
python -m tests.test_segment        # the part that finds where your sentence ends
python -m tests.test_loop_offline   # the full conversation, faked end to end
python -m tests.manual_backend_check  # one real cheap turn against Claude Code
```

## FAQ

**Will it read my secrets out loud?**
No. It only speaks the assistant's actual sentences. The boring machinery stays silent and on the screen.

**Does it work on Windows or Linux?**
The voice part is Mac-only today because it uses the built-in voice. Everything else is portable — the voice slot is swappable, it's just waiting on someone to fill it.

**Why not just type?**
Because my hands are busy and my mouth isn't.

**Is this going to hear my roommate and start talking to Claude?**
Crank `--energy-threshold` up. It'll ignore anything quieter than you leaning into the mic.

---

Built by [Nate](https://github.com/fidgetcoding) under the fidgetcoding flag. MIT licensed — take it, break it, make it weirder.

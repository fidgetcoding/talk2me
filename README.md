# talk2me 🎙️

**Talk to your terminal. It talks back. Then it shuts up and listens again — no buttons, no holding a key like it's a walkie-talkie.**

You say something. It hears you, types it to your coding agent, reads the answer out loud, and reopens the mic on its own. That's the whole loop. You never touch the keyboard unless you want to.

---

## Quick nav

- [Why I built this](#why-i-built-this)
- [What it actually does](#what-it-actually-does)
- [The trick](#the-trick)
- [Saying yes out loud](#saying-yes-out-loud)
- [Interrupting it](#interrupting-it)
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

## Saying yes out loud

A coding agent isn't just chat — sooner or later it wants to *do* something. Run a command. Write a file. In a terminal you'd get a y/n prompt. Hands-free, that used to be a dead end.

Now it's a conversation:

> **it:** "Claude wants to run the command npm install. Approve or deny?"
> **you:** "go ahead"
> *…it runs, and the answer keeps going.*

The rules, because a microphone is a terrible place for blind trust:

- **Boring, read-only stuff** (reading files, `git status`, running tests) just happens. No nagging.
- **Sharp stuff** (`sudo`, `git push`, deleting things, anything that phones out) is **hard-blocked**. It doesn't ask. You can't approve it by voice at all. That's on purpose — you shouldn't be able to `sudo` by mumbling.
- **Everything in between** gets the spoken question. Say "approve", "yes", "go ahead" — or "no", "stop", "deny". If it can't tell what you meant, it asks once more, then plays it safe and says no on your behalf.

Grow the quiet list as you go: `--allow-tool 'Bash(make:*)'` for things you're tired of approving, `--deny-tool` for things it should never ask about. `--no-voice-approval` turns the gate off entirely (blocked things just get declined silently).

The typed version works too: in `--text` mode the same gate is a plain `approve? [y/N]` prompt.

## Interrupting it

`talk2me --barge-in` — for when the answer is long and you already know where it's going.

Put on headphones (that part's mandatory — with speakers it hears its own voice and argues with itself), and the mic stays hot while it talks. Start speaking and it stops mid-sentence — not just the voice, the *thinking*: the agent's turn is actually cancelled, and what you said becomes the next message. Cutting it off mid-ramble feels rude the first time. It gets easier.

Default mode is still the polite half-duplex loop: it finishes, then listens. No headphones needed there.

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
| `talk2me --barge-in` | Headphones on, mic stays hot, you can cut it off mid-sentence. See [Interrupting it](#interrupting-it). |
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

- **Ears (voice detection):** a simple loudness check by default — good enough for a quiet room. Two sharper options ship too: `--vad webrtc` (install with `pip install -e ".[webrtc]"`) holds up much better across mics, especially Bluetooth; `--vad silero` runs a small neural model (needs `pip install onnxruntime` plus the [silero_vad.onnx](https://github.com/snakers4/silero-vad) model file).
- **Transcription:** Whisper, running on your machine. No cloud, no per-minute meter.
- **Voice:** the built-in macOS voice by default. `--tts kitten` (install with `pip install -e ".[kitten]"`) is a local neural voice that works on any OS. `--tts null` keeps it text-only.
- **The agent:** Claude Code.

## Not done yet

Being honest about the edges, because shipping half-true READMEs is how trust dies:

- **Wispr hands-free.** I want to drive the dictation app I actually like instead of the basics. It's a real maybe — the keypress trick it needs isn't proven yet.
- **Barge-in without headphones.** Full-duplex over speakers means hearing you over its own voice, and that echo handling isn't written. Headphones sidestep the whole problem, so that's the requirement for now.
- **Linux in the wild.** The headless tests run on Linux in CI, but nobody has driven the live mic loop there yet. See [Requirements](#requirements) for what should and shouldn't work.

## Requirements

Everywhere:

- **`claude`** on your PATH.
- **Python 3.11+**
- **A microphone**, ideally close to your face.

**macOS** — the platform this is built and daily-driven on. `pip install -e .` is all the setup there is: the audio library ships with PortAudio bundled, and the default voice is the built-in `say`.

**Linux** — honestly: the headless tests pass in CI, the live mic loop is untested. What's known:

- PortAudio is not bundled on Linux — install the system library first: `sudo apt-get install libportaudio2` (Debian/Ubuntu) or `sudo dnf install portaudio` (Fedora). It sits on top of ALSA/PulseAudio/PipeWire, whichever you have.
- There is no `say` on Linux, and the default voice **degrades to silence** rather than erroring. Pick a voice explicitly: `--tts kitten` (local neural voice, `pip install -e ".[kitten]"`) or `--tts null` (answers stay on screen).
- Whisper transcription and the `--text` mode are plain CPU Python and should work as-is.

**Windows** — untested, no built-in voice path. `--text` mode should work; everything else is unverified.

## Tests

Plain scripts. No frameworks to install. The whole loop is tested without a mic and without spending a cent on the agent:

```bash
python -m tests.test_segment        # the part that finds where your sentence ends
python -m tests.test_loop_offline   # the full conversation, faked end to end
python -m tests.test_permission     # the spoken approve/deny gate, all paths
python -m tests.test_barge_in       # interrupting it mid-sentence
python -m tests.manual_backend_check  # one real cheap turn against Claude Code
```

## FAQ

**Will it read my secrets out loud?**
No. It only speaks the assistant's actual sentences. The boring machinery stays silent and on the screen.

**Does it work on Windows or Linux?**
The default voice is Mac-only (`say`), but `--tts kitten` is a local neural voice that runs anywhere. On Linux you also need the system PortAudio library for the mic — see [Requirements](#requirements). Fair warning: the Linux mic loop is CI-tested, not human-tested.

**Why not just type?**
Because my hands are busy and my mouth isn't.

**Is this going to hear my roommate and start talking to Claude?**
Crank `--energy-threshold` up. It'll ignore anything quieter than you leaning into the mic.

---

Built by [Nate](https://github.com/fidgetcoding) under the fidgetcoding flag. MIT licensed — take it, break it, make it weirder.

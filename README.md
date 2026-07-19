# talk2me 🎙️

**Talk to your terminal. It talks back. Then it shuts up and listens again — no buttons, no holding a key like it's a walkie-talkie.**

You say something. It hears you, types it to your coding agent, reads the answer out loud, and reopens the mic on its own. That's the whole loop. You never touch the keyboard unless you want to.

---

## Quick nav

- [Install (30 seconds)](#install-30-seconds)
- [Why I built this](#why-i-built-this)
- [What it actually does](#what-it-actually-does)
- [The trick](#the-trick)
- [Reading the screen](#reading-the-screen-is-it-broken-or-is-it-waiting)
- [How long things take](#how-long-things-take)
- [Saying yes out loud](#saying-yes-out-loud)
- [Interrupting it](#interrupting-it)
- [Finish your sentence](#finish-your-sentence)
- [Hear Claude Code itself](#hear-claude-code-itself-hook-mode)
- [Quickstart](#quickstart)
- [Changing the voice](#changing-the-voice)
- [Switching the ears](#switching-the-ears)
- [Ways to run it](#ways-to-run-it)
- [The debugging playbook](#the-debugging-playbook)
- [Tuning the ears](#tuning-the-ears)
- [Under the hood](#under-the-hood)
- [Not done yet](#not-done-yet)
- [Requirements](#requirements)
- [Tests](#tests)
- [FAQ](#faq)

---

## Install (30 seconds)

One line, in any terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash
```

Then:

```bash
t2m
```

That's it. That's the command. Talk.

Want the fast GPU ears (Apple Silicon) baked in from the start? Same line, one flag:

```bash
curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/main/install.sh | bash -s -- --parakeet
```

Other ways in, if that's your style:

```bash
# pipx (isolated, on PATH, no thinking)
pipx install "git+https://github.com/fidgetcoding/talk2me.git"

# plain pip, your venv, your rules
pip install "git+https://github.com/fidgetcoding/talk2me.git"

# or from a clone
git clone https://github.com/fidgetcoding/talk2me.git && cd talk2me && pip install -e .
```

And the laziest path of all: paste this repo's URL into Claude Code and say *"install this."* You're about to have voice conversations with the thing — it can handle a pip install.

First run on macOS pops a **microphone permission** dialog for your terminal — click Allow, or you'll be talking to nobody.

## Why I built this

I live in Claude Code. All day, in the terminal, sometimes from my phone on the couch over SSH.

I already had half of a voice workflow: Wispr Flow gives me dictation, so my words get *in* fine. What I never had was the other half. The Claude app on my phone talks back. ChatGPT talks back. You say a thing, you hear a thing, the conversation has a pulse. My terminal? Silent. I'd dictate a prompt, then sit there reading a wall of text like it's 1997.

I wanted the full feedback loop — the thing the phone apps have — for my CLI: talk, *hear* the answer, and have it start listening again by itself. No push-to-talk, no reading. Nobody had built that for a terminal coding agent without scraping the screen like a raccoon going through a dumpster. So here we are.

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

One consequence worth knowing: talk2me runs its **own** Claude Code session (a headless one it controls), not the pretty interactive UI. If what you want is voice *inside* the normal Claude Code interface, that exists too — see [Hear Claude Code itself](#hear-claude-code-itself-hook-mode).

## Reading the screen (is it broken, or is it waiting?)

This is the section I wish every voice tool had. Voice interfaces fail silently — you talk, nothing happens, and you can't tell whether it's thinking, waiting for you, or dead. talk2me prints a marker for every state it's in. Learn these and you'll never wonder again:

| You see | It means | What to do |
|---|---|---|
| `(loading the ears…)` | Loading the transcription model. One-time, at startup, before the mic even opens. | Wait a few seconds. Don't talk yet. |
| `talk2me ready — start talking` | Mic is about to go live. | Talk whenever. |
| `(half-duplex: talking over the agent is ignored…)` | You did NOT pass `--barge-in`. Interrupting it mid-speech will do nothing this session. | Fine for most use. Restart with `--barge-in` + headphones if you want to interrupt. |
| `🎧 listening…` | Your turn. The mic is hot and idle. | Say something. |
| `▶ speech` *(only with `--debug`)* | It heard you start talking. | Keep going. |
| `⏹ turn end: ~1950ms speech -> transcribing` *(debug)* | You stopped, the silence window elapsed, your words are being transcribed. | Nothing — the answer is coming. |
| `⏹ … ignored (too short)` *(debug)* | It heard a blip under ~250ms (a cough, a key-click) and threw it away. | Nothing. This is noise rejection working. |
| `(…waiting for the rest)` | **Not a bug. Not stuck.** Your sentence sounded unfinished ("so what do you call…"), so it's holding the turn and giving you up to ~6 seconds to keep going. It'll do this up to 3 times. | Finish your sentence. Or stay silent and it sends what it has. |
| `🗣 you: …` | What it heard, final. This is exactly what the agent receives. | If it's wrong, just say "no, I said…" — it's a conversation. |
| `🤖` followed by streaming text | The agent is answering. Speech starts at the first clause, not the end. | Listen. |
| `[tool] Bash` etc. | The agent is using a tool. Shown, never spoken. Tool-heavy turns take longer before you hear anything. | Patience — watch the tools tick by. |
| `[permission] Bash: command=…` + a spoken question | The approval gate. The agent wants to run something and the turn is PAUSED until you answer. | Say "approve" or "deny". Unclear twice = auto-deny. |
| `[barge-in] listening…` | You talked over it (or something did). Playback and the agent's turn were cut; it's now collecting what you're saying. | Finish your sentence — it becomes the next message. |
| `[go on…]` | Same cut, but it happened before the agent said anything — it thinks you're still finishing YOUR sentence. | Keep talking; your fragments get stitched together. |
| `🗣 you (continued): …` | It stitched your interrupted sentence back together and sent the whole thing. | Nothing — this is the fix working. |
| `(noise interrupt — repeating your question)` | Something cut the turn but transcribed to nothing (a cough, a chair). It's re-sending your question instead of eating it. | Nothing. Self-healing. |
| `[t] stt / first-token / first-audio` *(debug)* | The latency receipts for the turn. | Use them when tuning; see the table below. |
| Long silence, no markers at all | NOW it might actually be stuck. | See [The debugging playbook](#the-debugging-playbook). |

The one-line version: **if the screen printed a marker recently, it's not broken — it's in whatever state the marker says.** The only bad state is no marker and no sound.

## How long things take

Measured on an M-series MacBook Pro, `--model haiku`, whisper base.en. Your numbers will vary; `--debug` prints yours per turn.

| Stage | Typical | The knob |
|---|---|---|
| You stop talking → turn ends | **1.2s** (fixed silence window) | `--silence-ms`. Lower = snappier but cuts off your thinking pauses. |
| Turn ends → transcript | 0.1–0.4s | `--stt parakeet` is the fast end. |
| Transcript → first token | 1.3–2.5s (simple), 3–7s+ (tool-using turns) | Model choice. Tool turns are just slower — watch the `[tool]` lines. |
| First token → first sound | 1–2s (TTS render of the first clause) | Shorter first sentences start faster; a streaming TTS engine is the future fix. |
| **Total: silence → hearing the answer** | **~4–6s** simple, more with tools | |
| "Waiting for the rest" window | up to 6s per pause, ×3 | Fixed. Silence ends it early. |
| Interrupting it (barge-in) | cuts within ~0.5s of sustained speech | 450ms threshold — a cough won't trigger it, a sentence will. |
| An interruption's max length | 10s, then it processes what it has | So a rant can't wedge the loop. |

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

Put on headphones (that part's mandatory — with speakers the mic hears its own voice and argues with itself), and the mic stays hot while it talks. Start speaking and it stops mid-sentence — not just the voice, the *thinking*: the agent's turn is actually cancelled, and what you said becomes the next message.

And if you forget the headphones? It notices. When the output device is open-air speakers (the system default with nothing plugged in), talk2me prints `🔈 speakers on the output — barge-in off for this session` and runs the polite mode instead of arguing with its own echo. Pop the headphones in, relaunch, and barge-in arms itself again. (One blind spot: a *Bluetooth speaker* has an arbitrary name and passes for headphones — don't use `--barge-in` with one.)

Know these three things and barge-in will never surprise you:

1. **There's no magic word.** "Stop" isn't special. ANY sustained speech (~half a second) cuts it — the whole sentence you said becomes the next instruction.
2. **It will cut on your mumbles too.** Think out loud near a hot mic and it'll stop talking and listen. That's the deal you signed.
3. **Interruptions cap at 10 seconds.** State your business; it processes what it has and moves on.

Default mode is still the polite half-duplex loop: it finishes, then listens. No headphones needed there.

## Finish your sentence

Real people trail off. "So what do you call… uh…" *(three seconds of staring at the ceiling)* "…a group of crows?"

Old voice tools send the fragment, and the agent answers garbage. talk2me listens for whether your sentence *sounded finished* — the transcription engines actually signal this (they skip the final period when you trail off) — and holds the turn:

```
⏹ turn end: ~2370ms speech -> transcribing
   (…waiting for the rest)        ← it knows you're not done
  ▶ speech                        ← you kept going
🗣  you: So what do you call a group of crows?    ← ONE turn, stitched
```

You get ~6 seconds per pause, up to 3 pauses. Once you start talking again it always hears you out — the window only limits the silence, never cuts a started sentence.

## Hear Claude Code itself (hook mode)

talk2me's main loop runs its own agent session. But maybe you *like* the regular Claude Code interface — the panels, the todo lists, your session history — and you just want the missing half: hearing the replies. (This was literally the original itch: Wispr for input, nothing for output.)

That's a one-file hook, and it ships in this repo:

```jsonc
// ~/.claude/settings.json — add to your hooks:
{"hooks": {"Stop": [{"hooks": [{"type": "command",
    "command": "python3 /path/to/talk2me/scripts/claude-speak.py", "timeout": 10}]}]}}
```

Every time Claude Code finishes a reply — in the real UI, your model, your session — the hook reads the transcript, strips the markdown, skips the code blocks ("code is on screen"), and speaks the reply.

It's **off by default** and gated by a toggle file, so background agents and scheduled jobs never start talking at 10 PM:

```bash
touch ~/.talk2me-speak     # voice ON  (new sessions)
rm ~/.talk2me-speak        # voice OFF
echo 'Ava (Premium)|236' > ~/.talk2me-speak   # ON, with a specific voice|rate
```

Replies over ~1200 characters get summarized to their first chunk plus "the rest is on screen." A new reply cuts off the previous one mid-sentence, like a person who has moved on.

Hook mode + Wispr (or any dictation) = full voice conversation inside the real Claude Code UI. talk2me's own loop = fully hands-free, mic and all. Pick per mood; they don't conflict.

## Quickstart

```bash
t2m
```

Start talking. Hit `Ctrl-C` when you're done. (`t2m` and `talk2me` are the same command — the short one exists because you'll type it a lot.)

No `--model` flag needed — it uses whatever your `claude` defaults to, same as the regular UI. Pass `--model haiku` when you're testing and don't want to spend real-model money on "count to fifty."

Got a favorite setup? Freeze it into an alias once and never think about flags again. Mine:

```bash
alias t2m='t2m --barge-in --voice "Ava (Premium)" --input-device "MacBook Pro"'
```

No `--output-device` needed — sound follows whatever macOS is currently routed to (headphones when they're in, laptop speakers when they're not, and barge-in [auto-disarms on speakers](#interrupting-it)). Pinning the *input* to the laptop mic is the one worth keeping: it spares your Bluetooth headphones from dropping into telephone-quality mode.

## Changing the voice

The stock macOS voice works, but it's giving 2005. Two minutes fixes that:

1. **System Settings → Accessibility → Read & Speak** (called *Spoken Content* on older macOS).
2. Click the **ⓘ next to "System voice"** — that's the whole catalog, with ▶ preview buttons.
3. Download anything tagged **(Enhanced)** or **(Premium)** — **Ava (Premium)** is the crowd favorite; **Evan (Enhanced)** if you want a male voice. 100–500MB each.
4. Tell talk2me: `t2m --voice "Ava (Premium)"`.

Speed is separate: `--rate 236` is the default (~1.35× the sleepy stock pace), `--rate 260` if you like it brisk. You don't need to change the system voice setting itself — the flag is enough.

## Switching the ears

Two transcription engines ship today, one flag apart:

| | **whisper** (default) | **parakeet** |
|---|---|---|
| Runs on | CPU, any Mac/Linux | Apple-Silicon GPU |
| Speed | ~0.3–0.4s per utterance | **~0.1–0.2s** |
| Accuracy | good | **better** — nails "orchestrator.py" |
| Custom vocab (`--vocab`) | ✅ | ❌ (raw accuracy compensates) |
| RAM while running | ~0.5 GB | ~2 GB |
| Install | included | `pip install -e ".[parakeet]"` (or the `--parakeet` installer flag) |

```bash
t2m                  # whisper
t2m --stt parakeet   # the fast ones
```

First parakeet run downloads the model (one-time). More engines are planned — everything hides behind the same contract, so adding one is a single file, and your muscle memory doesn't change.

## Ways to run it

| Command | What you get |
|---|---|
| `talk2me` | Just talk. The main event. |
| `talk2me --text` | Type instead of talk — for when you're in public and not ready to be the person speaking to their laptop. |
| `talk2me --barge-in` | Headphones on, mic stays hot, you can cut it off mid-sentence. See [Interrupting it](#interrupting-it). |
| `talk2me --debug` | Prints every ear-state and the per-turn latency receipts. Use this the first session, and any time something feels off. |
| `talk2me --voice "Ava (Premium)"` | A voice from this decade. Download it first: System Settings → Accessibility → Read & Speak → the ⓘ next to System voice → grab an (Enhanced)/(Premium) voice. |
| `talk2me --rate 260` | Talk faster (words/min). Default 236 ≈ 1.35× the macOS stock pace. |
| `talk2me --vocab Lorecraft --vocab Morgen` | Teach it your weird proper nouns so it stops inventing spellings. |
| `talk2me --stt parakeet` | The fast ears: Apple-Silicon GPU transcription — more accurate than any local Whisper and ~10× faster. `pip install -e ".[parakeet]"` first. |
| `talk2me --tts null` | Answers on screen, no voice out. |
| `talk2me --with-user-config` | Load your full user-level Claude config into the agent. Off by default because hooks and skills measurably slow every turn and their chatter gets read aloud. Project CLAUDE.md always loads. |

## The debugging playbook

Everything below actually happened while building this. If your session misbehaves, it's probably one of these — in rough order of likelihood:

**It never hears me at all — no `▶ speech` ever.**
macOS mic permission. The first run should pop a permission dialog for your terminal app; if you missed it: System Settings → Privacy & Security → Microphone → your terminal → on. Then restart talk2me. Still nothing? `talk2me --list-devices` and check the `*` is on the mic you're actually talking into, then `--input-device "MacBook"` (name substring works).

**I talk over it and nothing happens.**
You're not in barge-in mode. Check the startup banner: if you see the `(half-duplex: talking over the agent is ignored…)` line, the `--barge-in` flag wasn't passed. If you run through a shell alias, remember an open terminal doesn't reload edited aliases — `source ~/.zshrc` or open a new tab, then verify with `alias talk2me` / `alias t2m` that the flag is really in there. (This one got me twice in one night.)

**It cuts itself off / interrupts on nothing / stops mid-answer.**
The mic is hearing its own voice. Barge-in **requires** the audio to go to your ears only — if the headphones are on the desk instead of on your head, the mic hears the TTS, decides "someone's talking," and cuts the answer. Wear the headphones or drop `--barge-in`.

**It cuts me off mid-sentence when I pause to think.**
Raise `--silence-ms` (default 1200). And note the fallbacks already working for you: unfinished-sounding sentences get the `(…waiting for the rest)` hold, and if it does send early and you keep talking, the fragments get stitched (`you (continued):`).

**Bluetooth audio sounds like a telephone from 1995.**
The classic Bluetooth trap: using a BT headset's *microphone* forces the whole headset into telephone-quality mode. Split the devices — `--output-device "AirPods" --input-device "MacBook"` — and you get full-quality audio out with the laptop mic in. (Your AirPods show up under whatever name you gave them. Mine are "Megapods".)

**Every turn is slow and it recites my global config / says weird meta things.**
That was hook and CLAUDE.md inheritance — fixed by default now (the agent loads only project-level config; measured: it halved time-to-first-token). If you passed `--with-user-config`, that's the tradeoff you opted into.

**It answered but I hear nothing, screen looks fine.**
Wrong output device (check `--output-device` / `--list-devices`), or macOS routed audio elsewhere when a BT device connected mid-session. Restart talk2me after changing audio devices — PortAudio's device list is a snapshot.

**First utterance after startup takes forever.**
It shouldn't anymore — the model loads before the mic opens (`(loading the ears…)`). If you see that line for more than ~10s on parakeet, it's downloading the model from Hugging Face (first run only, ~600MB+).

**The count-to-fifty test sounds mushy.**
Digit runs at speed are the hardest thing you can ask a TTS voice to say. The stock voice garbles them; a Premium voice (Ava) mostly doesn't. This is a voice-quality ceiling, not a bug — the streaming-TTS engine on the roadmap is the real fix.

**How do I know it's actually broken?**
No new marker on screen, no sound, for longer than the timing table says it should take — *and* you're not in a `(…waiting for the rest)` or `[permission]` state. Then: Ctrl-C (twice if needed), rerun with `--debug`, reproduce, and read the last marker printed — that's the state it died in. The `[t]` lines tell you which stage ate the time.

## Tuning the ears

The default mic settings assume a quiet room and a close mic. If it keeps cutting you off, or won't trigger at all, turn two knobs:

```bash
talk2me --debug --energy-threshold 0.02   # higher = needs you louder (kills background noise)
talk2me --debug --silence-ms 1500          # higher = waits longer before deciding you're done
```

Run with `--debug` while you tune. You'll see exactly when it thinks you started and stopped, so you're adjusting on evidence instead of vibes.

## Under the hood

Every swappable part hides behind a contract: voice detection, transcription, the voice it speaks in, and the agent on the other end. Want a better transcriber or a different voice? That's one new file, not a heart transplant. The main loop never even knows it changed.

Out of the box:

- **Ears (voice detection):** a simple loudness check by default — good enough for a quiet room. Two sharper options ship too: `--vad webrtc` (install with `pip install -e ".[webrtc]"`) holds up much better across mics, especially Bluetooth; `--vad silero` runs a small neural model (needs `pip install onnxruntime` plus the [silero_vad.onnx](https://github.com/snakers4/silero-vad) model file).
- **Transcription:** Whisper, running on your machine. No cloud, no per-minute meter. Or `--stt parakeet` (install with `pip install -e ".[parakeet]"`): NVIDIA's Parakeet on the Apple-Silicon GPU — better accuracy than any local Whisper at a tenth of the wait, for ~2 GB of RAM while it runs. English-only, and `--vocab` biasing stays a Whisper-only trick. The full research behind the tradeoff lives in `docs/stt-upgrade-research.md`.
- **The mouth:** macOS `say`, rendered a chunk ahead of playback so the voice doesn't stall between sentences. `--tts kitten` (install with `pip install -e ".[kitten]"`) is a local neural voice that works on any OS. `--tts null` keeps it text-only.
- **The agent:** Claude Code, over its structured streaming interface — including the tool-permission wire, which is how the spoken approval gate works. The wire format is pinned byte-for-byte in `docs/permission-spike-results.md`.
- **The brain-to-ear glue:** after every reply, the identifiers the agent just used (file names, function names) get fed to the transcriber as bias terms — because those are exactly the words you're about to say back.

## Not done yet

Being honest about the edges, because shipping half-true READMEs is how trust dies:

- **A streaming voice.** `say` renders each chunk fully before playing it, so there's a hard prosody reset at chunk boundaries — audible on long lists. A neural streaming TTS (Kokoro is the candidate) removes it. That's the next build.
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
python -m tests.test_continuation   # "wait, I wasn't done" stitching
python -m tests.test_flow           # run-on speech chunking + unfinished-sentence holds
python -m tests.manual_backend_check  # one real cheap turn against Claude Code
```

## FAQ

**Will it read my secrets out loud?**
No. It only speaks the assistant's actual sentences. The boring machinery stays silent and on the screen.

**Why does it run its own session instead of living inside the normal Claude Code UI?**
Because the interactive UI has no programmatic way in, and screen-scraping it is exactly the brittleness this project exists to avoid. The structured pipe is the reliable path — and for the "I just want the real UI, plus sound" case, [hook mode](#hear-claude-code-itself-hook-mode) does that.

**Does it work on Windows or Linux?**
The default voice is Mac-only (`say`), but `--tts kitten` is a local neural voice that runs anywhere. On Linux you also need the system PortAudio library for the mic — see [Requirements](#requirements). Fair warning: the Linux mic loop is CI-tested, not human-tested.

**Why not just type?**
Because my hands are busy and my mouth isn't.

**Is this going to hear my roommate and start talking to Claude?**
Crank `--energy-threshold` up. It'll ignore anything quieter than you leaning into the mic.

---

Built by [Nate](https://github.com/fidgetcoding) under the fidgetcoding flag. MIT licensed — take it, break it, make it weirder.

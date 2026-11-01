# talk2me v2.7.0: everything on my "isn't proven" list

I keep a list of what doesn't work or isn't proven. This release is me going after all of it.

[FILL BEFORE TAGGING: one line on the live test, e.g. "I've now talked to this build on a real mic and a real iPhone." Delete anything it didn't pass.]

## Do this if you're upgrading

- **Voice-lock users: run `t2m --enroll-voice` again.** Your old voiceprint keeps working the old way, and each launch reminds you, but you won't get the fix below until you re-enroll.
- **Everything else carries over.** base.en is still the default ears, nothing new downloads on first run, and your saved setup stays as you saved it.

## What's new

**A real wall: `t2m --sandbox`.** The denylist reads command lines, so a script walks right past it. This has the OS enforce it, with `sandbox-exec` on macOS and bubblewrap on Linux. The agent can write only to the project and reach only the model API, and that covers its own Write tool too. It's off by default because `npm install` and friends then need `--sandbox-allow-host`. I tested it by running a hostile script at it, and [SECURITY.md](../SECURITY.md) has what it stops and what it can't.

**Voice-lock works now.** For three versions it mostly just observed, and I blamed laptop mics. The real cause was my bug: I was feeding the speaker model the wrong features. On recordings of 40 real speakers through a simulated laptop room:

| | Before | After |
|---|---|---|
| Speakers it enforces for | 4 of 40 | 39 of 40 |
| Strangers let through | 93.5% | 6.3% |
| Owner rejected | 2.7% | 9.2% |

Most of those owner rejections are on clips of 1.5 seconds or less. It's a filter, not a lock: anything under 0.8 seconds always passes.

**Voice commands in five more languages.** Spanish, French, German, Italian and Portuguese, typed or spoken, approvals included. English always works. On synthetic voices, single command words land about 85 to 90% of the time, and a miss just asks again. Its own spoken replies are still English.

**The dead air is gone from the default voice.** Chunks render side by side now. On a 10-sentence reply measured on files, the worst stop went from 380-880 ms to 16 ms or less, and restarts from 9 to 4 or 5. `say` still resets its intonation at each restart.

**Speaking on Linux.** With no `say` there, talk2me uses espeak-ng if you've got it. It sounds like a robot, and it beats silence.

**Fewer tracebacks.** No `claude` on PATH, a bad `--cwd`, a busy phone port, no PortAudio, a mistyped model: each is one sentence now. `--language es` on the default model moves to the multilingual one instead of hearing Spanish as English gibberish. `--version` exists, and `--help` starts by saying what this is.

**A broken part costs that part, not the session.** A pulled headset used to hang it for good, and an error from the speech engine ended it. Now each says what happened once and carries on. One malformed line from the agent costs that line. Escape codes in agent text no longer reach your terminal.

**`--help` comes in sections,** and Ctrl-D at a setup prompt cancels instead of crashing.

**Bugs the new tests caught:** a crash on Ctrl-C on Linux, four separate Windows failures, and a phone hang-up on a torn packet.

## How I tested it

- **Live mic loop on Linux:** real PortAudio and PulseAudio on virtual sound cards, in CI.
- **Windows:** every headless suite, plus the real CLI through real pipes, in CI. That says nothing about a live mic on Windows.
- **Phone page:** real browsers over a real SSH tunnel, in CI. Chromium ran the whole path. WebKit's Linux build connects, but has no fake mic.
- **The ears on real people:** two openly licensed sets, LibriSpeech and Speech Commands, both CC BY 4.0, from files.

| Engine | Word errors | Gate right | Opposite verdict | False approvals (of 600) |
|---|---|---|---|---|
| whisper base.en (default) | 5.6% | 92.5% | 0 | 6 |
| large-v3-turbo, GPU | 3.5% | 93.0% | 0 | 1 |
| parakeet v2, GPU | 1.7% | 75.0% | 1 | 23 |

Parakeet is the best at sentences and the worst at one word, so think twice before pairing it with the spoken gate.

## What's still not proven

- Windows and Linux with a real mic.
- Real accents in the new languages.
- A long session under `--sandbox`, where the login token refreshes partway through.
- Neither dataset has coding talk on a laptop mic in a room.

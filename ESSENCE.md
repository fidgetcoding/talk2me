# talk2me: the essence

**What it is.** A hands-free voice loop for terminal coding agents. You talk, it transcribes on your own machine, hands your words to Claude Code, speaks the answer out loud, and reopens the mic by itself. No push-to-talk, no reading walls of text. Python, MIT, built and used daily on macOS.

**Who it's for.** People who live in Claude Code (or OpenAI's Codex CLI) and would rather have a conversation than squint at a terminal. People who want to keep building with their hands off the keyboard, including from a phone on the couch over SSH.

**Why it exists.** I already had dictation, so my words got in fine. Nothing came back out. Every turn was hit Fn, talk, release, wait, read, hit Fn again. The phone apps talk back and my terminal didn't, and nobody had built the whole loop for a terminal agent without scraping the screen.

**What's different.**

- It owns the agent's structured stream instead of reading the terminal, so it only ever speaks the actual answer. Tool calls, diffs and file paths stay on screen and never get read aloud.
- You can talk over it on open laptop speakers. On a Mac it uses the echo canceller FaceTime uses, so its own voice can't cut it off and yours can. Everywhere else, a userspace echo gate does the same job with less headroom.
- It asks out loud before running anything. A mic hears the TV too, so the spoken gate is the default, and a 69-rule denylist sits on top in every mode.
- Every part swaps behind a contract: ears, voice, brain, renderer. Three speech engines ship, and Codex runs through the same loop with `--agent codex`.
- It prints a marker for every state it's in, so silence is never a mystery. You can always tell "thinking" from "waiting for you" from "stuck".

**How to try it.** You need `claude` on your PATH, Python 3.11 or newer, and a mic.

```bash
curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.6.0/install.sh | bash
cd your-project
t2m
```

The first launch walks you through a short setup and asks for mic permission. The default ears are Whisper base.en on the CPU, a one-time download of about 150 MB. Every flag and every fix is in the [README](README.md).

**What works today.** Each number below comes from a commit, a doc in this repo, or CI.

- **By voice, on my MacBook Pro:** the whole loop, spoken approvals, cutting in mid-answer, voice pause and wake, and typing mid-session. I built small games and fixed their bugs by talking to it (private notes, July 18 to 20, 2026).
- **Talking over open speakers:** with macOS's echo canceller on, a check with real speakers, a hot mic and a scripted agent got 0 self-cuts in 4 of 4 runs, counting and prose, at volume 45 and 100 ([AEC plan](docs/v2.5-aec-plan.md)). In my field test it caught every talk-over up to four volume clicks below max, about 75%.
- **Speed:** about 4 to 6 seconds from the moment you stop talking to hearing a simple answer, on an M-series MacBook Pro with haiku and base.en ([timing table](README.md#how-long-things-take)).
- **The ears, on recordings of real people:** 40 LibriSpeech speakers, 4,367 words. Word errors were 5.6% for base.en, 3.5% for GPU Whisper and 1.7% for Parakeet. On single spoken words, the approval gate got 92.5% right on base.en with zero opposite verdicts (`20783fb`).
- **CI on every push:** 30 headless suites on Python 3.11, 3.12 and 3.13 and on Windows, the live voice loop on Linux virtual sound cards (16 checks), the phone page in real browsers over a real SSH tunnel (18 checks), and `--sandbox` through the real `claude` CLI on macOS and Linux. All 9 jobs were green on the newest commit on `main` ([run 41326272035](https://github.com/fidgetcoding/talk2me/actions/runs/41326272035)).
- **On `main` since v2.6.0, not tagged yet** (`--ref main` gets it): an opt-in OS sandbox, voice commands in five more languages, and a voice-lock fix. On 40 real speakers through a simulated laptop room, voice-lock went from letting 93.5% of strangers through to 6.3%, and it rejects its own owner 9.2% of the time, mostly on the shortest clips (`e6ce7b0`).

That `main`-only work has only heard files so far, and phone mode has never run on a real iPhone. How it got here, echo war included, is in [EVOLUTION.md](EVOLUTION.md).

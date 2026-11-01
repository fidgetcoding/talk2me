# talk2me, on one page

**What it is.** A hands-free voice loop for terminal coding agents. You talk, it transcribes, your agent answers out loud, and the mic reopens on its own. Local ears, local voice, and whichever agent you already use. Python, MIT.

**Who it's for.** People who live in Claude Code (or Codex, Kimi, GLM, DeepSeek) and want a conversation instead of a wall of text. Anyone who wants to build with their hands off the keyboard, including from a phone over SSH.

**Why it exists.** I already had dictation. Each turn was still "hit Fn, talk, release, wait, read output, hit Fn again". The phone apps talk back and my terminal didn't. Nobody had built the full loop for a terminal agent without scraping the screen.

**What's different.**
- It never scrapes the terminal. It owns the agent's structured stream, so it only ever speaks the actual answer, never menus or diffs.
- Voice asks before running anything. A mic hears the TV, so spoken approval is the default, with a 69-rule denylist on top in every mode. `--sandbox` is opt-in and has the OS enforce the limits, on macOS and Linux.
- You can talk over it, on headphones or open speakers, using macOS's own echo canceller.
- Every swappable part sits behind a contract: ears, voice, agent, renderer.

**How to try it.** `curl -fsSL https://raw.githubusercontent.com/fidgetcoding/talk2me/v2.7.0/install.sh | bash`, then `t2m` from your project folder. You need `claude` on your PATH, Python 3.11+ and a mic. Default ears are Whisper base.en on the CPU. GPU Whisper (`--stt whisper-gpu`) and Parakeet are opt-in, on Apple Silicon.

## What works today

- **The loop, by voice:** spoken approvals, barge-in, voice pause and wake, session resume, typing mid-session, the settings wizard and transcripts. I've driven these by voice on macOS, including building and fixing small games.
- **Talking over speakers:** macOS's native echo cancellation, field-tested up to about 75% volume.
- **CI on every push:** 27 headless suites on three Python versions and on Windows, the live voice loop on Linux with virtual sound cards, the phone page in real browsers over a real SSH tunnel, and `--sandbox` through the real `claude` CLI on macOS and Linux.
- **Ears on recordings of real people:** on audiobook readers, word errors were 5.6% for base.en, 3.5% for GPU Whisper and 1.7% for Parakeet. On single spoken words, the approval gate got 92.5% right on base.en, with no opposite verdicts. These are files, not a mic in a room.
- **Voice-lock:** on recordings of 40 real speakers through a simulated laptop room, strangers let in fell from 93.5% to 6.3%, and it now enforces for 39 of the 40. The owner gets rejected 9.2% of the time, mostly on the shortest clips. It's off until you enroll, and it's a filter, not a lock: anything under 0.8 seconds always passes. It hasn't heard a real voice through a real mic since the fix.
- **Commands in six languages:** English plus Spanish, French, German, Italian and Portuguese, typed or spoken. On synthetic voices, single command words landed 68 of 80 times on `--whisper-model base`.

Everything added since v2.6.0 ran in CI and from files.

See [EVOLUTION.md](EVOLUTION.md) for how it got here.

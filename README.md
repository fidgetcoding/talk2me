# talk2me

Talk to your terminal coding agent. No push-to-talk, no copy-paste, no leaving the keyboard to tap a phone.

talk2me is a PTY-agnostic voice broker: launch it inside any terminal (Ghostty, Terminal.app, iTerm2, Alacritty, kitty, WezTerm, tmux) and it runs a turn-taking voice conversation with a terminal agent — Claude Code by default. You talk, it transcribes and feeds the agent, the agent's reply is spoken back, then it listens again.

## How it works

One long-lived `claude` process in bidirectional stream-json. talk2me owns stdin (so it can inject your spoken turns) and reads structured events from stdout (so only the assistant's *prose* is spoken — tool calls are shown but never read aloud). That structured transport is what makes a clean voice broker possible instead of scraping a TUI.

```
mic → VAD segment → whisper → claude (stream-json) → spoken reply → listen again
```

Everything swappable sits behind a Protocol (`talk2me/protocols.py`): VAD, STT, TTS, and the agent backend. Swapping whisper for Deepgram, or `say` for ElevenLabs, is a new class plus one branch in `factory.py` — never an orchestrator change.

## Run

```bash
pip install -e .

talk2me --model haiku                 # voice loop
talk2me --text --model haiku          # type instead of talk (no audio)
talk2me --vocab Lorecraft --vocab Morgen   # bias STT toward names it'd mangle
talk2me --tts null                    # show text, no spoken output
```

Defaults: half-duplex (mic muted while the agent speaks — no echo-cancellation hardware needed), energy VAD, local faster-whisper STT, macOS `say` TTS.

## Tuning

Energy VAD is a quiet-room baseline. If it cuts you off or won't trigger:

```bash
talk2me --energy-threshold 0.02   # higher = needs louder speech
talk2me --silence-ms 1200         # longer = waits more before ending your turn
```

## Requirements

- macOS (TTS uses the built-in `say`)
- `claude` CLI on PATH
- Python ≥ 3.11

## Tests

Plain scripts, no pytest needed. The whole loop is exercised headless (no mic, no LLM cost):

```bash
python -m tests.test_segment        # turn segmenter, pure logic
python -m tests.test_loop_offline   # full orchestrator with fakes
python -m tests.manual_backend_check  # real claude spine check (one cheap turn)
```

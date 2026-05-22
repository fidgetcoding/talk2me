"""talk2me — hands-free voice loop for terminal coding agents.

A PTY-agnostic voice broker: launch it inside any terminal (Ghostty, Apple
Terminal, iTerm2, Alacritty, kitty, WezTerm, tmux). It owns a turn-taking voice
conversation with a terminal agent (Claude Code by default) — you talk, it
transcribes and feeds the agent, the agent's reply is spoken back, then it
listens again. No push-to-talk.
"""

__version__ = "0.1.0"

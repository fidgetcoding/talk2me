"""talk2me — hands-free voice loop for terminal coding agents.

A PTY-agnostic voice broker: launch it inside any terminal (Ghostty, Apple
Terminal, iTerm2, Alacritty, kitty, WezTerm, tmux). It owns a turn-taking voice
conversation with a terminal agent (Claude Code by default) — you talk, it
transcribes and feeds the agent, the agent's reply is spoken back, then it
listens again. No push-to-talk.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("talk2me")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "unknown"

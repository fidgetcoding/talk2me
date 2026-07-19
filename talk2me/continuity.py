"""Session continuity — `t2m --continue` picks up where you left off.

Every launch records its agent session id keyed by the working directory
(~/.talk2me/state.json). `--continue` looks the last one up and resumes it
via the CLI's `--resume` (spike-verified: state survives, the id persists),
so the agent remembers the game you built an hour ago instead of arguing
that it doesn't exist (live-observed).
"""

from __future__ import annotations

import json
import os

STATE_KEEP = 50  # most-recent working dirs remembered


def _state_path() -> str:
    return os.environ.get("TALK2ME_STATE") or os.path.expanduser(
        "~/.talk2me/state.json"
    )


def _key(cwd: str | None) -> str:
    return os.path.realpath(os.path.expanduser(cwd or os.getcwd()))


def load_last_session(cwd: str | None) -> str | None:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    entry = data.get("sessions", {}).get(_key(cwd))
    return entry.get("id") if isinstance(entry, dict) else None


def save_last_session(cwd: str | None, session_id: str) -> None:
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        data = {}
    sessions = data.setdefault("sessions", {})
    # seq is a monotonic recency counter — no wall clock (stable + testable).
    seq = data.get("seq", 0) + 1
    data["seq"] = seq
    sessions[_key(cwd)] = {"id": session_id, "seq": seq}
    if len(sessions) > STATE_KEEP:
        for stale in sorted(sessions, key=lambda k: sessions[k].get("seq", 0))[
            : len(sessions) - STATE_KEEP
        ]:
            del sessions[stale]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError:
        pass  # continuity is best-effort; never break a launch over it

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


HISTORY_KEEP = 10  # sessions remembered per working dir (for the picker)


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _store(data: dict) -> None:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    except OSError:
        pass  # continuity is best-effort; never break a launch over it


def save_last_session(cwd: str | None, session_id: str) -> None:
    data = _load()
    sessions = data.setdefault("sessions", {})
    # seq is a monotonic recency counter — no wall clock (stable + testable).
    seq = data.get("seq", 0) + 1
    data["seq"] = seq
    entry = sessions.setdefault(_key(cwd), {})
    entry["id"] = session_id
    entry["seq"] = seq
    # Per-dir history for the spoken session picker (newest last; deduped).
    history = [h for h in entry.get("history", []) if h.get("id") != session_id]
    history.append({"id": session_id, "seq": seq, "title": "", "started": ""})
    entry["history"] = history[-HISTORY_KEEP:]
    if len(sessions) > STATE_KEEP:
        for stale in sorted(sessions, key=lambda k: sessions[k].get("seq", 0))[
            : len(sessions) - STATE_KEEP
        ]:
            del sessions[stale]
    _store(data)


def record_title(cwd: str | None, session_id: str, title: str) -> None:
    """Attach a human handle to a session — its first user message. Only the
    first call sticks; also stamps the start time for the picker display."""
    import time

    data = _load()
    entry = data.get("sessions", {}).get(_key(cwd))
    if not isinstance(entry, dict):
        return
    for h in entry.get("history", []):
        if h.get("id") == session_id and not h.get("title"):
            h["title"] = title.strip()[:70]
            h["started"] = time.strftime("%a %I:%M %p")
            _store(data)
            return


def list_sessions(cwd: str | None, exclude: str | None = None) -> list[dict]:
    """Sessions for this dir, newest first, minus the one currently running.
    Untitled entries (never got a first message) are noise — dropped."""
    data = _load()
    entry = data.get("sessions", {}).get(_key(cwd))
    if not isinstance(entry, dict):
        return []
    out = [
        h
        for h in reversed(entry.get("history", []))
        if h.get("id") and h.get("id") != exclude and h.get("title")
    ]
    return out[:HISTORY_KEEP]

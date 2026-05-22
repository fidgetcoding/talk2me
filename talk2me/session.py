"""Session persistence — remember the last conversation across runs.

The Claude Code backend mints a fresh `--session-id` UUID on every launch
(`ClaudeCodeBackend.__init__` falls back to `uuid.uuid4()` when no id is
passed), so each `python -m talk2me` invocation starts a brand-new
conversation. To let a user pick up where they left off, we persist the id of
the last session to disk and read it back on the next run.

This module owns only the storage layer — read/write/validate. It deliberately
does NOT import the backend, config, or CLI: those are owned elsewhere and the
wiring below is for whoever owns them, not for this file to perform.

CLI / backend wiring (to be added by the owners of __main__.py and
claude_code.py — this module does not touch either):

1. __main__.py — add a `--resume` flag and feed the stored id into Config /
   the backend factory::

       p.add_argument(
           "--resume", action="store_true",
           help="continue the last conversation instead of starting fresh",
       )
       ...
       resume_id = None
       if a.resume:
           last = load_last_session()
           resume_id = last["session_id"] if last else None
       # pass resume_id through Config -> factory.build_backend ->
       # ClaudeCodeBackend(session_id=resume_id). When None, the backend mints
       # a fresh UUID exactly as it does today (no behavior change).

   Note: Claude Code's `--session-id` (re)starts a session under a chosen id;
   if a future CLI exposes a true `--resume <id>` resume flag, swap the
   backend arg accordingly. The persisted id is the join key either way.

2. claude_code.py — after the `system/init` event yields `SessionReady`, the
   backend (or the orchestrator that consumes events) records the live id::

       from ..session import save_session

       if isinstance(ev, SessionReady) and ev.session_id:
           save_session(ev.session_id, cwd=self._cwd, model=self._model)

   `SessionReady.session_id` is the authoritative id (Claude Code echoes the
   one it actually used), so we save *that* rather than the requested id.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["session_store_path", "save_session", "load_last_session"]

_STORE_FILENAME = "last-session.json"
_DEFAULT_HOME = "~/.talk2me"

# Loose UUID shape: 8-4-4-4-12 hex groups, case-insensitive. We validate shape
# only — we don't enforce a UUID version, since Claude Code may echo ids we did
# not mint and we'd rather store a plausible id than reject a valid one.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def _talk2me_home() -> Path:
    """Resolve the talk2me state directory.

    Honors ``$TALK2ME_HOME`` when set (and non-empty); otherwise defaults to
    ``~/.talk2me``. The returned path is expanded but not created — callers that
    write are responsible for ``mkdir``.
    """
    raw = os.environ.get("TALK2ME_HOME", "").strip() or _DEFAULT_HOME
    return Path(raw).expanduser()


def session_store_path() -> Path:
    """Absolute path to the persisted last-session file.

    Defaults to ``~/.talk2me/last-session.json`` and respects ``$TALK2ME_HOME``.
    """
    return (_talk2me_home() / _STORE_FILENAME).resolve()


def _is_plausible_uuid(session_id: str) -> bool:
    """True if ``session_id`` matches the canonical 8-4-4-4-12 hex UUID shape."""
    return bool(_UUID_RE.match(session_id))


def save_session(
    session_id: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
) -> None:
    """Persist the most recent session id (plus context) atomically.

    Writes ``{session_id, cwd, model, saved_at}`` to :func:`session_store_path`.
    ``saved_at`` is an ISO-8601 UTC timestamp. Parent directories are created as
    needed. The write is atomic: content lands in a temp file in the same
    directory and is then ``os.replace``'d over the target, so a reader never
    observes a half-written file and a crash mid-write cannot corrupt the store.

    Raises:
        ValueError: if ``session_id`` is not a plausible UUID.
    """
    if not isinstance(session_id, str) or not _is_plausible_uuid(session_id):
        raise ValueError(f"not a plausible session UUID: {session_id!r}")

    target = session_store_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "model": model,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(payload, indent=2, ensure_ascii=False)

    # Temp file in the SAME directory guarantees os.replace is atomic (a rename
    # across filesystems is not). delete=False so we control the replace.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_STORE_FILENAME}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure; never mask the
        # original error.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def load_last_session() -> dict[str, Any] | None:
    """Load the persisted last session, or ``None`` if unavailable.

    Returns the parsed payload dict (``session_id``, ``cwd``, ``model``,
    ``saved_at``). Returns ``None`` — never raising — when the store is missing,
    unreadable, not valid JSON, not a JSON object, or lacks a plausible
    ``session_id``. Treating a corrupt store as "no prior session" keeps a
    bad file from wedging startup; the next :func:`save_session` overwrites it.
    """
    path = session_store_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(parsed, dict):
        return None

    session_id = parsed.get("session_id")
    if not isinstance(session_id, str) or not _is_plausible_uuid(session_id):
        return None

    return parsed


if __name__ == "__main__":
    # Round-trip smoke test against a throwaway dir so we never touch a real
    # ~/.talk2me store. Sets $TALK2ME_HOME, saves, loads, and asserts equality.
    import sys

    with tempfile.TemporaryDirectory() as tmp_home:
        os.environ["TALK2ME_HOME"] = tmp_home

        store = session_store_path()
        print(f"store path: {store}")
        assert str(store).startswith(os.path.realpath(tmp_home)), "path not under TALK2ME_HOME"

        sid = str(uuid.uuid4())
        save_session(sid, cwd="/Users/nathandavidovich/code/talk2me", model="sonnet")
        assert store.exists(), "save_session did not create the store"

        loaded = load_last_session()
        print(f"loaded: {loaded}")
        assert loaded is not None, "load returned None after save"
        assert loaded["session_id"] == sid, "session_id round-trip mismatch"
        assert loaded["cwd"] == "/Users/nathandavidovich/code/talk2me", "cwd mismatch"
        assert loaded["model"] == "sonnet", "model mismatch"
        assert "saved_at" in loaded, "saved_at missing"

        # Corrupt-store tolerance: bad JSON must yield None, not raise.
        store.write_text("{ not valid json", encoding="utf-8")
        assert load_last_session() is None, "corrupt store should load as None"

        # Bad UUID must be rejected on save.
        try:
            save_session("not-a-uuid")
        except ValueError:
            pass
        else:
            raise AssertionError("save_session accepted a non-UUID")

    print("PASS")
    sys.exit(0)

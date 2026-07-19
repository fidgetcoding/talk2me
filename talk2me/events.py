"""Backend-agnostic event types flowing from an AgentBackend to the orchestrator.

The orchestrator never sees raw stream-json. A backend translates whatever its
underlying agent emits into this closed set, so swapping Claude Code for another
agent runtime is a single-file change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionReady:
    """Agent session is initialized and ready for the first user turn."""

    session_id: str | None


@dataclass(frozen=True)
class AssistantTextDelta:
    """A chunk of assistant prose. Spoken aloud. Accumulate + sentence-chunk for TTS."""

    text: str


@dataclass(frozen=True)
class ToolActivity:
    """The agent invoked a tool. Shown in the transcript, never spoken."""

    name: str
    summary: str = ""


@dataclass(frozen=True)
class TurnComplete:
    """The agent finished responding to the current user turn. Hand the mic back."""

    # Plain-text rollup of the turn's assistant prose, if the backend provides it.
    text: str = ""


@dataclass(frozen=True)
class PermissionRequest:
    """The agent wants to use a tool and the backend is blocked awaiting an answer.

    The host must call `backend.respond_permission(request_id, allow=...)` —
    the underlying turn is paused until it does (verified: no CLI-side timeout,
    so a voice round-trip is safe; see docs/permission-spike-results.md).
    """

    request_id: str
    tool_name: str
    tool_input: dict


@dataclass(frozen=True)
class BackendError:
    """The backend hit an unrecoverable condition (process died, parse wedged)."""

    message: str


# Union of everything a backend may yield.
AgentEvent = (
    SessionReady
    | AssistantTextDelta
    | ToolActivity
    | TurnComplete
    | PermissionRequest
    | BackendError
)

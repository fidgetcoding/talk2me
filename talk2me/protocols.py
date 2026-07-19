"""Provider contracts. Every swappable piece implements one of these.

Perfection target: the orchestrator depends only on these Protocols, never on a
concrete provider. Adding ElevenLabs TTS or Deepgram STT later means writing a
new class that satisfies the contract — no orchestrator changes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

import numpy as np

from .events import AgentEvent


@runtime_checkable
class VAD(Protocol):
    """Voice-activity detector. Frame-by-frame speech probability."""

    sample_rate: int
    frame_samples: int  # samples per frame this VAD expects

    def is_speech(self, frame: np.ndarray) -> bool:
        """True if `frame` (float32 mono, len == frame_samples) contains speech."""
        ...

    def reset(self) -> None:
        """Clear internal state between utterances."""
        ...


@runtime_checkable
class STT(Protocol):
    """Speech-to-text. Transcribes a complete utterance (post-VAD-segmentation)."""

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Return the transcript of `audio` (float32 mono). May be empty."""
        ...


@runtime_checkable
class TTS(Protocol):
    """Text-to-speech. Streams synthesized audio for a text chunk.

    Yields float32 mono PCM blocks at `sample_rate`. Yielding incrementally lets
    the player start before synthesis finishes (low perceived latency) and lets
    barge-in cancel mid-utterance.
    """

    sample_rate: int

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        """Yield PCM blocks for `text`."""
        ...
        yield  # pragma: no cover  (typing: marks this an async generator)


@runtime_checkable
class AgentBackend(Protocol):
    """The thing on the other end of the conversation (e.g. Claude Code)."""

    async def start(self) -> None:
        """Spawn / connect the agent. Must precede send()."""
        ...

    async def send(self, user_text: str) -> None:
        """Submit a user turn. Resulting events arrive via events()."""
        ...

    def events(self) -> AsyncIterator[AgentEvent]:
        """Long-lived stream of normalized agent events across all turns."""
        ...

    async def interrupt(self) -> None:
        """Best-effort: stop the in-flight turn. No-op if unsupported."""
        ...

    async def respond_permission(
        self, request_id: str, allow: bool, *, message: str | None = None
    ) -> None:
        """Answer a PermissionRequest event. No-op for backends that never emit one."""
        ...

    async def close(self) -> None:
        """Tear down the agent process / connection."""
        ...

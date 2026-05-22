"""No-audio TTS. Satisfies the TTS Protocol but emits no PCM.

For headless runs where you want the agent's text on screen but no spoken audio
(e.g. driving the real loop over SSH on a box with a mic but no speaker, or
profiling the loop without waiting on synthesis).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np


class NullTTS:
    sample_rate: int = 16000

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        return
        yield  # pragma: no cover  (marks this an async generator)

"""Energy (RMS) voice-activity detector. Zero-dependency baseline.

Cheap and good enough for a quiet room with a close mic. For noisy environments
swap in a Silero VAD behind the same Protocol — the orchestrator never changes.
"""

from __future__ import annotations

import numpy as np


class EnergyVAD:
    """RMS-threshold VAD. A frame is speech if its energy clears `threshold`."""

    def __init__(
        self,
        *,
        sample_rate: int,
        frame_samples: int,
        threshold: float = 0.012,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.threshold = threshold

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        return rms >= self.threshold

    def reset(self) -> None:
        # Stateless detector; nothing to clear.
        return None

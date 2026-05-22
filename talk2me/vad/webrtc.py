"""WebRTC voice-activity detector. Battle-tested, light, device-robust.

Google's WebRTC VAD (via the `webrtcvad` C extension) classifies 10/20/30 ms
frames of 16-bit PCM as speech or not. Unlike the RMS `EnergyVAD`, it's not a
raw-loudness threshold — so it holds up far better across microphones with
different gain and noise floors (the Bluetooth-headset case, where a fixed
energy threshold tuned on the built-in mic misfires). No torch, no onnx: a ~30 KB
C extension behind the same `VAD` Protocol.

Install: `pip install talk2me[webrtc]` (pulls `webrtcvad-wheels`, prebuilt wheels
that build cleanly on modern Python where the original `webrtcvad` sdist does not).
"""

from __future__ import annotations

import numpy as np

try:
    import webrtcvad
except Exception:  # pragma: no cover - optional dependency
    webrtcvad = None

# WebRTC VAD only accepts these sample rates and frame durations. Anything else
# raises inside the C extension, so we validate up front with a clear message.
_VALID_RATES = (8000, 16000, 32000, 48000)
_VALID_FRAME_MS = (10, 20, 30)


class WebrtcVAD:
    """WebRTC-backed VAD. A frame is speech per Google's GMM classifier.

    `aggressiveness` (0..3) trades sensitivity for noise rejection: 0 lets the
    most through (captures quiet speech, more false positives), 3 filters the
    hardest (rejects noise, may clip soft speech). 2 is a good default for a
    close mic in a normal room.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        frame_samples: int,
        aggressiveness: int = 2,
    ) -> None:
        if webrtcvad is None:
            raise RuntimeError(
                "webrtcvad not installed — run `pip install talk2me[webrtc]` "
                "(or `pip install webrtcvad-wheels`)"
            )
        if sample_rate not in _VALID_RATES:
            raise ValueError(
                f"webrtc VAD needs sample_rate in {_VALID_RATES}, got {sample_rate}"
            )
        frame_ms = frame_samples * 1000 / sample_rate
        if int(frame_ms) not in _VALID_FRAME_MS or frame_ms != int(frame_ms):
            raise ValueError(
                f"webrtc VAD needs a 10/20/30 ms frame; {frame_samples} samples "
                f"@ {sample_rate} Hz is {frame_ms} ms"
            )
        if not 0 <= aggressiveness <= 3:
            raise ValueError(f"aggressiveness must be 0..3, got {aggressiveness}")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame: np.ndarray) -> bool:
        # webrtcvad demands an exact-length frame. A short/long frame (e.g. a
        # trailing partial at stream end) is treated as non-speech rather than
        # crashing the C call.
        if frame.size != self.frame_samples:
            return False
        # float32 [-1, 1] mono -> 16-bit little-endian PCM bytes.
        pcm = np.clip(frame, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2").tobytes()
        return self._vad.is_speech(pcm16, self.sample_rate)

    def reset(self) -> None:
        # The WebRTC VAD carries no cross-frame state we own; nothing to clear.
        return None

"""Echo-aware barge gating — speakers full-duplex without hearing itself.

The identity approach (voice-lock echo-guard) turned out infeasible on real
laptop mics: same-speaker embedding scores scatter wider than the gap to a
stranger (live-measured 2026-07-19: owner 0.17-0.63, a video 0.24). This
module solves the actual requirement deterministically instead: we KNOW the
exact PCM the Speaker is playing, so any mic sound that isn't explained by
that reference is, by definition, not our own voice — and may barge.

Two pieces:

- ``EchoRef`` — a ring buffer of recently played samples, fed by the Speaker
  as blocks go to the output stream.
- ``EchoGate.foreign(mic_audio)`` — "is there sound here beyond our own
  echo?" Envelope-domain matching (10 ms RMS hops): find the best-aligned,
  best-gain fit of the reference envelope inside the mic envelope, subtract
  it, and measure what's left. Envelope matching survives the nonlinear
  distortion of small laptop speakers that wrecks waveform-level AEC.

This is detection, not cancellation: we never need cleaned audio, only the
barge decision. After a cut the TTS stops, so the utterance tail that reaches
the transcriber is naturally echo-free.
"""

from __future__ import annotations

import threading

import numpy as np

# Envelope hop. 10 ms is fine enough to track speech energy contours and
# coarse enough that a few-ms alignment error between reference and echo
# lands in the same hop.
_HOP_S = 0.010

# Ring capacity. Must cover the longest mic window we analyze (4 s) plus the
# playback pipeline's write-ahead lead and the acoustic path.
_RING_S = 6.0

# How much EXTRA reference (beyond the mic window) to search for alignment.
# Output-stream buffering means the write cursor leads the air by up to a few
# hundred ms; the room adds a few more.
_MAX_LEAD_S = 0.8

# Analyze at most this much of the mic window (its tail). Longer utterances
# than this can't be pure echo of a recent sentence anyway.
_MAX_ANALYSIS_S = 4.0

# Reference below this RMS = "nothing playing": every sound is foreign.
_REF_SILENCE_RMS = 1e-4

# Mic below this RMS = nothing there at all (the VAD shouldn't have fired).
_MIC_SILENCE_RMS = 1e-4

# Foreign verdict: the residual (mic envelope minus best-fit echo) must be a
# real fraction of the mic energy AND above an absolute floor. Set against
# the synthetic fixtures in tests/test_echogate.py: pure echo through tanh
# distortion + 120ms delay leaves ~0.05 residual; a QUIET second voice over
# it leaves ~0.43. 0.30 splits that with headroom on both sides for real
# rooms (reverb tails raise the echo residual; loud talk-overs raise the
# foreign one).
_RESIDUAL_FRAC = 0.30
_RESIDUAL_ABS = 3e-4


def _envelope(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """RMS envelope at _HOP_S hops. Empty input -> empty envelope."""
    hop = max(1, int(sample_rate * _HOP_S))
    n = audio.shape[0] // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    x = audio[: n * hop].astype(np.float32).reshape(n, hop)
    return np.sqrt(np.mean(x * x, axis=1))


class EchoRef:
    """Thread-safe ring of recently played PCM (float32 mono).

    The Speaker feeds it from play(); the gate reads it from a worker thread
    (speech checks run via asyncio.to_thread), hence the lock.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._cap = int(sample_rate * _RING_S)
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._pos = 0  # next write index
        self._written = 0  # total samples ever written
        self._lock = threading.Lock()

    def add(self, block: np.ndarray) -> None:
        b = np.asarray(block, dtype=np.float32).reshape(-1)
        if b.shape[0] == 0:
            return
        if b.shape[0] >= self._cap:
            b = b[-self._cap :]
        with self._lock:
            end = self._pos + b.shape[0]
            if end <= self._cap:
                self._buf[self._pos : end] = b
            else:
                first = self._cap - self._pos
                self._buf[self._pos :] = b[:first]
                self._buf[: end - self._cap] = b[first:]
            self._pos = end % self._cap
            self._written += b.shape[0]

    def recent(self, seconds: float) -> np.ndarray:
        """The last `seconds` of played audio, oldest-first.

        When less has ever been written, the front is ZERO-padded to the full
        requested length — silence is literally what preceded the first
        block, and the pad is what keeps the gate's alignment search
        full-range at the START of a sentence. (Live bug 2026-07-19: a
        truncated return collapsed the search to one misaligned lag on the
        first sentence of a turn, the fit failed, and the agent cut itself
        off at "1" of "count to 30".) A never-written ring still returns
        empty so callers can tell 'nothing has ever played'."""
        n = min(int(seconds * self.sample_rate), self._cap)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        with self._lock:
            if self._written <= 0:
                return np.zeros(0, dtype=np.float32)
            have = min(n, self._written) if self._written < self._cap else n
            start = (self._pos - have) % self._cap
            if start + have <= self._cap:
                tail = self._buf[start : start + have].copy()
            else:
                first = self._cap - start
                tail = np.concatenate(
                    (self._buf[start:], self._buf[: have - first])
                )
        if have < n:
            tail = np.concatenate(
                (np.zeros(n - have, dtype=np.float32), tail)
            )
        return tail


class EchoGate:
    """foreign(mic_audio, sample_rate) -> True when the mic holds sound the
    playback reference can't explain. Pure numpy; safe on worker threads."""

    def __init__(self, ref: EchoRef) -> None:
        self.ref = ref
        # Last computed residual fraction, surfaced for --debug lines.
        self.last_residual: float | None = None

    def foreign(self, mic_audio: np.ndarray, sample_rate: int) -> bool:
        self.last_residual = None
        mic = np.asarray(mic_audio, dtype=np.float32).reshape(-1)
        max_n = int(_MAX_ANALYSIS_S * sample_rate)
        if mic.shape[0] > max_n:
            mic = mic[-max_n:]
        if mic.shape[0] == 0:
            return False
        mic_rms = float(np.sqrt(np.mean(mic * mic)))
        if mic_rms < _MIC_SILENCE_RMS:
            return False

        mic_dur = mic.shape[0] / sample_rate
        ref = self.ref.recent(mic_dur + _MAX_LEAD_S)
        if ref.shape[0] == 0 or float(np.sqrt(np.mean(ref * ref))) < _REF_SILENCE_RMS:
            return True  # nothing playing — all sound is foreign

        m = _envelope(mic, sample_rate)
        r = _envelope(ref, self.ref.sample_rate)
        if m.shape[0] == 0:
            return False
        if r.shape[0] < m.shape[0]:
            r = np.concatenate((np.zeros(m.shape[0] - r.shape[0], dtype=r.dtype), r))

        # Best-gain least-squares fit of the reference envelope at every lag;
        # keep the lag whose residual is smallest (equivalently, the best
        # explanation of the mic by the playback).
        best_res = None
        lags = r.shape[0] - m.shape[0] + 1
        mm = float(np.dot(m, m))
        for lag in range(lags):
            seg = r[lag : lag + m.shape[0]]
            rr = float(np.dot(seg, seg))
            if rr <= 0.0:
                continue
            g = max(0.0, float(np.dot(m, seg)) / rr)
            res = m - g * seg
            np.clip(res, 0.0, None, out=res)
            res_e = float(np.dot(res, res))
            if best_res is None or res_e < best_res:
                best_res = res_e
        if best_res is None:
            return True  # reference envelope empty at every lag
        frac = float(np.sqrt(best_res / mm)) if mm > 0 else 0.0
        self.last_residual = frac
        res_rms = float(np.sqrt(best_res / m.shape[0]))
        return frac > _RESIDUAL_FRAC and res_rms > _RESIDUAL_ABS

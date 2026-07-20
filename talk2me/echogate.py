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
import time

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
# real fraction of the mic energy AND above an absolute floor. FIELD-tuned:
# Nate's MacBook (2026-07-19 session logs) put real own-echo residuals at
# 0.30 and 0.41 — synthetic echo fixtures sit at 0.05-0.18, i.e. real rooms
# roughly double the lab numbers. 0.50 clears the observed echo range;
# talk-overs at comparable loudness measure 0.6+ (fixtures). The price is
# that a WHISPERED talk-over under loud TTS may not cut — speak up, or the
# echo-transcript backstop catches any false cut that still slips.
_RESIDUAL_FRAC = 0.50

# The gain fit is PIECEWISE — one gain per ~250ms segment, clamped to a
# spread around the segments' median. One global gain false-triggered on
# real hardware (live 2026-07-19, three self-barges on prose answers): the
# mic's AGC/room compression squashes loud syllables, so dynamic speech
# can't be explained by a single scale factor and its own echo read as
# foreign — while the monotone counting test sailed through. Per-segment
# gains absorb that slow gain warp; the clamp keeps a foreign voice from
# being 'explained' by wild gain jumps.
_SEG_HOPS = 25  # 250ms at the 10ms hop
_GAIN_SPREAD = 3.0
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
    """Thread-safe ring of recently played PCM (float32 mono) on a WALL-CLOCK
    timeline: silence between writes is recorded as zeros.

    That timeline is load-bearing. A pure sample-appender compresses out the
    gaps between sentences and between turns, so a mic window spanning a
    sentence boundary — [end of sentence 1][render pause][start of sentence
    2] — can't be explained by the gapless reference at ANY single alignment,
    and the agent's own voice reads as foreign (live 2026-07-19: it barged on
    itself and transcribed its own reply back as a user message).

    The Speaker feeds it from play(); the gate reads it from a worker thread
    (speech checks run via asyncio.to_thread), hence the lock.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._cap = int(sample_rate * _RING_S)
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._pos = 0  # next write index
        self._written = 0  # total samples ever appended (audio + gap zeros)
        # Wall time of the ring head (the end of the last appended content).
        # Writes pace at ~real time, so add-time is the block's start and
        # head-time advances by the block's duration.
        self._clock = time.monotonic()
        self._lock = threading.Lock()

    def _append_locked(self, b: np.ndarray) -> None:
        if b.shape[0] >= self._cap:
            b = b[-self._cap :]
        end = self._pos + b.shape[0]
        if end <= self._cap:
            self._buf[self._pos : end] = b
        else:
            first = self._cap - self._pos
            self._buf[self._pos :] = b[:first]
            self._buf[: end - self._cap] = b[first:]
        self._pos = end % self._cap
        self._written += b.shape[0]

    def add(self, block: np.ndarray) -> None:
        b = np.asarray(block, dtype=np.float32).reshape(-1)
        if b.shape[0] == 0:
            return
        now = time.monotonic()
        with self._lock:
            gap_s = now - self._clock
            if gap_s > 0.02:
                gap_n = min(int(gap_s * self.sample_rate), self._cap)
                self._append_locked(np.zeros(gap_n, dtype=np.float32))
            self._append_locked(b)
            self._clock = now + b.shape[0] / self.sample_rate

    def recent(self, seconds: float) -> np.ndarray:
        """The last `seconds` of the playback TIMELINE ending now, oldest-
        first: ring audio, plus zeros for the silence since the last write
        (the live tail-gap), plus front zero-padding when less has ever been
        written — silence is literally what preceded the first block, and the
        pad keeps the gate's alignment search full-range at the START of a
        sentence. (Live bug 2026-07-19: a truncated return collapsed the
        search to one misaligned lag on the first sentence of a turn, the fit
        failed, and the agent cut itself off at "1" of "count to 30".) A
        never-written ring still returns empty so callers can tell 'nothing
        has ever played'."""
        n = min(int(seconds * self.sample_rate), self._cap)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        now = time.monotonic()
        with self._lock:
            if self._written <= 0:
                return np.zeros(0, dtype=np.float32)
            tail_gap = min(
                int(max(0.0, now - self._clock) * self.sample_rate), n
            )
            want = n - tail_gap  # samples of actual ring content
            have = (
                min(want, self._written) if self._written < self._cap else want
            )
            if have > 0:
                start = (self._pos - have) % self._cap
                if start + have <= self._cap:
                    tail = self._buf[start : start + have].copy()
                else:
                    first = self._cap - start
                    tail = np.concatenate(
                        (self._buf[start:], self._buf[: have - first])
                    )
            else:
                tail = np.zeros(0, dtype=np.float32)
        parts = []
        if have < n - tail_gap or have <= 0:
            parts.append(np.zeros(n - tail_gap - max(have, 0), dtype=np.float32))
        parts.append(tail)
        if tail_gap > 0:
            parts.append(np.zeros(tail_gap, dtype=np.float32))
        return np.concatenate(parts) if len(parts) > 1 else parts[0]


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

        # Best piecewise-gain fit of the reference envelope at every lag;
        # keep the lag whose residual is smallest (the best explanation of
        # the mic by the playback).
        best_res = None
        lags = r.shape[0] - m.shape[0] + 1
        mm = float(np.dot(m, m))
        for lag in range(lags):
            seg = r[lag : lag + m.shape[0]]
            if float(np.dot(seg, seg)) <= 0.0:
                continue
            res_e = _piecewise_residual(m, seg)
            if best_res is None or res_e < best_res:
                best_res = res_e
        if best_res is None:
            return True  # reference envelope empty at every lag
        frac = float(np.sqrt(best_res / mm)) if mm > 0 else 0.0
        self.last_residual = frac
        res_rms = float(np.sqrt(best_res / m.shape[0]))
        return frac > _RESIDUAL_FRAC and res_rms > _RESIDUAL_ABS


def _piecewise_residual(m: np.ndarray, seg: np.ndarray) -> float:
    """Residual energy of `m` after subtracting the best per-segment-gain
    fit of `seg`, gains clamped to a spread around their median."""
    n = m.shape[0]
    bounds = [(a, min(a + _SEG_HOPS, n)) for a in range(0, n, _SEG_HOPS)]
    gains = []
    for a, b in bounds:
        rr = float(np.dot(seg[a:b], seg[a:b]))
        g = max(0.0, float(np.dot(m[a:b], seg[a:b])) / rr) if rr > 0.0 else 0.0
        gains.append(g)
    positive = [g for g in gains if g > 0.0]
    if positive:
        gmed = float(np.median(positive))
        if gmed > 0.0:
            lo, hi = gmed / _GAIN_SPREAD, gmed * _GAIN_SPREAD
            gains = [min(max(g, lo), hi) if g > 0.0 else 0.0 for g in gains]
    res_e = 0.0
    for (a, b), g in zip(bounds, gains):
        res = np.clip(m[a:b] - g * seg[a:b], 0.0, None)
        res_e += float(np.dot(res, res))
    return res_e

"""macOS voice-processing capture (AUVoiceIO) behind the Mic interface.

`VoiceProcessingMic` swaps the PortAudio input for an AVAudioEngine input
node with voice processing enabled — the same driver-level echo canceller
behind FaceTime. The OS subtracts EVERYTHING the Mac plays (Phase-0 spike:
the HAL-level reference covers PortAudio playback too, so the Speaker stays
PortAudio) and hands back a mic signal where the machine's own TTS is not
just quiet but unintelligible — whisper transcribes none of it and Silero
refuses to call it speech. Speakers barge-in stops needing the userspace
echo gate at all.

Spike-pinned facts this module is built around (docs/v2.5-aec-plan.md):

- Enabling voice processing flips the input node's format (observed:
  1ch/48k -> 9ch/48k deinterleaved, every channel identical). Take ch0 and
  resample to the pipeline's 16k.
- The engine is a PROCESS SINGLETON. Repeated enable/teardown cycles in one
  process wedged the input into digital silence (-180dBFS) after ~4 rounds;
  one engine, enabled once, restarted as needed, never did. Ctrl-T
  relaunches therefore reuse the engine rather than rebuilding it.
- A dead VPIO input produces EXACT zeros, while a healthy quiet room sits
  near -48dBFS — so `probe()` can tell "working" from "wedged" by peak
  amplitude alone, and the mode ladder can fall back to the echo gate
  before the session starts.
"""

from __future__ import annotations

import sys
import threading

import numpy as np

from .audio import Mic

# Peak below this = digital silence = the VPIO input is not delivering audio.
# A live mic's room floor measured ~-48dBFS (peak ~5e-3); true zeros only
# ever appeared on a wedged input.
_DEAD_PEAK = 1e-7

_PROBE_SECONDS = 0.4
_TAP_BUFFER = 4096

_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def is_available() -> bool:
    """True when this platform can even try native voice processing."""
    if sys.platform != "darwin":
        return False
    try:
        import AVFoundation  # noqa: F401  (pyobjc-framework-AVFoundation)
    except Exception:
        return False
    return True


def _shared_engine():
    """The process-lifetime AVAudioEngine with voice processing enabled.

    Created (and voice-processing-enabled) exactly once; callers start and
    stop it around sessions but never tear it down — see the module
    docstring for the wedge this avoids.
    """
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            from AVFoundation import AVAudioEngine

            engine = AVAudioEngine.alloc().init()
            inp = engine.inputNode()
            ok, err = inp.setVoiceProcessingEnabled_error_(True, None)
            if not ok:
                raise RuntimeError(f"voice processing refused: {err}")
            _ENGINE = engine
        return _ENGINE


def probe() -> bool:
    """Spin the engine briefly; True iff the tap delivers live audio.

    This is the mode ladder's go/no-go: it catches the platforms where
    AVFoundation imports but the voice-processing input is broken (or
    wedged), so a session never launches with a silent mic. Also serves as
    warmup — the engine it starts is the one the session will reuse.
    """
    if not is_available():
        return False
    try:
        engine = _shared_engine()
        inp = engine.inputNode()
        fmt = inp.outputFormatForBus_(0)
        need = float(fmt.sampleRate()) * _PROBE_SECONDS
        got = {"peak": 0.0, "frames": 0}
        done = threading.Event()

        def tap(buf, when) -> None:  # noqa: ANN001
            n = int(buf.frameLength())
            ch = buf.floatChannelData()
            if n == 0 or ch is None:
                return
            x = np.array(ch[0].as_tuple(n), dtype=np.float32)
            got["peak"] = max(got["peak"], float(np.max(np.abs(x))))
            got["frames"] += n
            if got["frames"] >= need:
                done.set()

        inp.installTapOnBus_bufferSize_format_block_(0, _TAP_BUFFER, fmt, tap)
        try:
            engine.prepare()
            ok, _err = engine.startAndReturnError_(None)
            if not ok:
                return False
            done.wait(_PROBE_SECONDS + 2.0)
        finally:
            inp.removeTapOnBus_(0)
            engine.stop()
        return got["frames"] > 0 and got["peak"] > _DEAD_PEAK
    except Exception:
        return False


class _FirDecimator:
    """Integer-ratio downsampler (windowed-sinc FIR + stride), stateful
    across arbitrarily-sized chunks so a stream can be fed piecewise with
    no boundary artifacts: feeding one big array or many small ones yields
    identical output."""

    def __init__(self, ratio: int, taps: int = 63) -> None:
        if ratio < 1:
            raise ValueError("ratio must be >= 1")
        self.ratio = ratio
        if ratio == 1:
            self._kernel = None
            return
        # Cutoff at 45% of the destination Nyquist — comfortably inside the
        # band whisper/VAD care about, sharp enough to kill aliasing.
        fc = 0.45 / ratio
        n = np.arange(taps) - (taps - 1) / 2
        k = np.sinc(2 * fc * n) * np.hamming(taps)
        self._kernel = (k / k.sum()).astype(np.float32)
        self._hist = np.zeros(taps - 1, dtype=np.float32)
        # Raw-rate offset (within the next chunk) of the next output sample.
        self._next = 0

    def feed(self, chunk: np.ndarray) -> np.ndarray:
        if self._kernel is None:
            return chunk
        x = np.concatenate([self._hist, chunk.astype(np.float32, copy=False)])
        y = np.convolve(x, self._kernel, mode="valid")  # len == len(chunk)
        out = y[self._next :: self.ratio]
        consumed = y.shape[0]
        self._next = (self._next - consumed) % self.ratio
        self._hist = x[-(self._kernel.shape[0] - 1) :]
        return out.astype(np.float32, copy=False)


class _LinearResampler:
    """Non-integer-ratio fallback (e.g. a 44.1k voice-processing format).
    Linear interpolation with a continuous time base across chunks — the
    same guard stt/whisper.py uses, kept stateful for streaming."""

    def __init__(self, src_rate: float, dst_rate: int) -> None:
        self.src_rate = float(src_rate)
        self.dst_rate = int(dst_rate)
        self._tail = np.zeros(1, dtype=np.float32)
        self._have_tail = False
        # Source-domain time (in samples) of the next output sample,
        # relative to the first sample of the next chunk fed.
        self._t = 0.0

    def feed(self, chunk: np.ndarray) -> np.ndarray:
        chunk = chunk.astype(np.float32, copy=False)
        if not self._have_tail:
            self._tail = chunk[-1:].copy() if chunk.size else self._tail
            self._have_tail = chunk.size > 0
            x = chunk
            base = 0.0
        else:
            x = np.concatenate([self._tail, chunk])
            base = -1.0  # x[0] sits one source sample before the chunk
            if chunk.size:
                self._tail = chunk[-1:].copy()
        if x.size < 2:
            return np.zeros(0, dtype=np.float32)
        step = self.src_rate / self.dst_rate
        # Output times within x's index space:
        start = self._t - base
        n_out = int(max(0.0, (x.size - 1 - start) / step)) + 1
        if n_out <= 0:
            self._t -= chunk.size
            return np.zeros(0, dtype=np.float32)
        t = start + np.arange(n_out) * step
        t = t[t <= x.size - 1]
        out = np.interp(t, np.arange(x.size), x).astype(np.float32)
        # Advance the time base past this chunk.
        last = t[-1] if t.size else start - step
        self._t = (last + step + base) - chunk.size
        return out


class VoiceProcessingMic(Mic):
    """Mic-compatible capture off the voice-processing input node.

    Interface parity with `audio.Mic` (start/stop/frames/set_muted/
    switch_device) is the whole point — the orchestrator cannot tell which
    one it holds. Frames are float32 mono `frame_samples` long at
    `sample_rate`, muted frames are discarded at the source, and the same
    bounded drop-oldest queue semantics apply (inherited).
    """

    def __init__(
        self, sample_rate: int, frame_samples: int, device: int | None = None
    ) -> None:
        # Mic.__init__ builds the queue/mute/drop bookkeeping; it opens no
        # stream (that happens in start()), so inheriting it is safe.
        super().__init__(sample_rate, frame_samples, device=device)
        self._engine = None
        self._tap_installed = False
        self._resampler = None
        self._pending = np.zeros(0, dtype=np.float32)

    # -- capture plumbing ---------------------------------------------------

    def _ingest(self, chunk: np.ndarray) -> None:
        """Resample a raw tap chunk and enqueue full frames. Runs on the tap
        thread; only the enqueue hops to the event loop. Split out from the
        tap block so headless tests can drive it directly."""
        if self._muted:
            # Parity with Mic: muted audio is discarded at the source, not
            # buffered — unmuting must never replay speech from the past.
            self._pending = np.zeros(0, dtype=np.float32)
            return
        if self._resampler is not None:
            chunk = self._resampler.feed(chunk)
        if chunk.size == 0:
            return
        buf = np.concatenate([self._pending, chunk])
        n = self.frame_samples
        whole = (buf.shape[0] // n) * n
        frames, self._pending = buf[:whole], buf[whole:]
        loop = self._loop
        if loop is None:
            return
        for i in range(0, whole, n):
            loop.call_soon_threadsafe(self._enqueue, frames[i : i + n].copy())

    def _open_stream(self) -> None:  # overrides the PortAudio path
        engine = _shared_engine()
        inp = engine.inputNode()
        fmt = inp.outputFormatForBus_(0)
        src_rate = float(fmt.sampleRate())
        if src_rate <= 0:
            raise RuntimeError("voice-processing input reports no sample rate")
        ratio = src_rate / self.sample_rate
        if abs(ratio - round(ratio)) < 1e-9:
            self._resampler = (
                None if round(ratio) == 1 else _FirDecimator(int(round(ratio)))
            )
        else:
            self._resampler = _LinearResampler(src_rate, self.sample_rate)
        self._pending = np.zeros(0, dtype=np.float32)

        def tap(buf, when) -> None:  # noqa: ANN001
            n = int(buf.frameLength())
            ch = buf.floatChannelData()
            if n == 0 or ch is None:
                return
            self._ingest(np.array(ch[0].as_tuple(n), dtype=np.float32))

        inp.installTapOnBus_bufferSize_format_block_(0, _TAP_BUFFER, fmt, tap)
        self._tap_installed = True
        engine.prepare()
        ok, err = engine.startAndReturnError_(None)
        if not ok:
            inp.removeTapOnBus_(0)
            self._tap_installed = False
            raise RuntimeError(f"voice-processing engine failed to start: {err}")
        self._engine = engine

    def stop(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            if self._tap_installed:
                engine.inputNode().removeTapOnBus_(0)
                self._tap_installed = False
            # Stop but never tear down — the singleton survives for the next
            # session in this process (Ctrl-T relaunch).
            engine.stop()

    def switch_device(self, device: int | None) -> None:
        """Native voice processing follows the system default input; there is
        no per-device selection to honor. Recorded, deliberately not acted
        on — the mode ladder refuses native AEC when an explicit
        --input-device is in play, so this only fires for hotkey cycling."""
        self.device = device

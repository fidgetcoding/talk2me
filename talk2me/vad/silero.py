"""Silero voice-activity detector — neural VAD via onnxruntime (NO torch).

A real model where EnergyVAD's RMS threshold falls apart: noisy rooms, open mics,
background hum. Same Protocol shape as EnergyVAD, so the orchestrator never knows
which one it got — swapping is a one-line factory branch.

Runtime cost is contained: onnxruntime and the model file are loaded lazily on
the first `is_speech` call, so importing this module stays cheap (and works even
when the model isn't installed). Silero runs on 16 kHz mono in a fixed window
(512 samples @ 16 kHz); we buffer/resample incoming frames internally so the
caller can keep using whatever `frame_samples` the mic produces.

Wiring this in (lead-owned files — do NOT edit them from here):

  talk2me/factory.py `build_vad`, mirroring the `if cfg.vad == "energy":` block:

      if cfg.vad == "silero":
          from .vad.silero import SileroVAD

          return SileroVAD(
              sample_rate=cfg.sample_rate,
              frame_samples=frame_samples(cfg),
              threshold=cfg.silero_threshold,
              model_path=cfg.silero_model_path,
          )

  talk2me/config.py `Config`, in the VAD / turn-detection block:

      silero_threshold: float = 0.5  # speech probability cutoff (0..1)
      silero_model_path: str | None = None  # ONNX path; None → bundled/env default

  talk2me/vad/__init__.py — optional convenience re-export:

      from .silero import SileroVAD  # add "SileroVAD" to __all__

Model file: download `silero_vad.onnx` from
https://github.com/snakers4/silero-vad (files/silero_vad.onnx). Point at it with
`silero_model_path=...`, the env var `SILERO_VAD_MODEL`, or drop it next to this
module as `silero_vad.onnx`. onnxruntime is required: `pip install onnxruntime`.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# Silero is trained at 16 kHz and reads a fixed-size window per inference step.
_MODEL_SAMPLE_RATE = 16000
_WINDOW_SAMPLES = 512  # samples the model consumes per step at 16 kHz
_STATE_SHAPE = (2, 1, 128)  # v5 unified recurrent state (replaces v4 h/c)
_DEFAULT_MODEL_NAME = "silero_vad.onnx"
_ENV_MODEL_VAR = "SILERO_VAD_MODEL"


class SileroVAD:
    """Neural VAD. A frame is speech if the model's probability clears `threshold`.

    Stateful: the model carries recurrent state across frames within an utterance.
    Call `reset()` between utterances to clear it.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        frame_samples: int,
        threshold: float = 0.5,
        model_path: str | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        if frame_samples <= 0:
            raise ValueError(f"frame_samples must be positive, got {frame_samples}")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")

        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.threshold = threshold
        self._model_path = model_path

        # Lazy: filled on first is_speech() so import stays cheap and model-free.
        self._session: object | None = None
        self._input_names: list[str] | None = None
        self._uses_unified_state: bool = True

        # Recurrent state + a sample buffer for partial windows (16 kHz domain).
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)  # v4 fallback
        self._c = np.zeros((2, 1, 64), dtype=np.float32)  # v4 fallback
        self._buffer = np.zeros(0, dtype=np.float32)

    def is_speech(self, frame: np.ndarray) -> bool:
        """True if `frame` (float32 mono, len == frame_samples) contains speech.

        Buffers and resamples internally to the model's 16 kHz / 512-sample window,
        runs the model on every full window the frame completes, and reports speech
        if any window's probability clears `threshold`.
        """
        if frame.size == 0:
            return False
        if self._session is None:
            self._load()

        samples = self._to_model_rate(np.asarray(frame, dtype=np.float32).ravel())
        self._buffer = np.concatenate((self._buffer, samples))

        speech = False
        while self._buffer.shape[0] >= _WINDOW_SAMPLES:
            window = self._buffer[:_WINDOW_SAMPLES]
            self._buffer = self._buffer[_WINDOW_SAMPLES:]
            if self._infer(window) >= self.threshold:
                speech = True
        return speech

    def reset(self) -> None:
        """Clear the model's recurrent state and any buffered samples."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)
        self._buffer = np.zeros(0, dtype=np.float32)

    # --- internals ---

    def _load(self) -> None:
        """Import onnxruntime and open the model. Raises with a clear fix on miss."""
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - exercised without ort
            raise RuntimeError(
                "SileroVAD needs onnxruntime. Install it with: pip install onnxruntime"
            ) from exc

        path = self._resolve_model_path()
        if path is None or not Path(path).is_file():
            raise RuntimeError(
                "Silero VAD model not found. Download silero_vad.onnx from "
                "https://github.com/snakers4/silero-vad and point at it via "
                "model_path=, the SILERO_VAD_MODEL env var, or place it next to "
                f"{__file__} as {_DEFAULT_MODEL_NAME}."
            )

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        session = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = [i.name for i in session.get_inputs()]
        # v5 uses a single "state" input; v4 uses separate "h"/"c".
        self._uses_unified_state = "state" in self._input_names
        self._session = session
        self.reset()

    def _resolve_model_path(self) -> str | None:
        """Pick the model file: explicit arg → env var → sibling default."""
        if self._model_path:
            return self._model_path
        env = os.environ.get(_ENV_MODEL_VAR)
        if env:
            return env
        sibling = Path(__file__).with_name(_DEFAULT_MODEL_NAME)
        return str(sibling) if sibling.is_file() else None

    def _infer(self, window: np.ndarray) -> float:
        """Run one 512-sample window through the model, threading recurrent state."""
        assert self._session is not None  # _load() ran first
        x = window.reshape(1, _WINDOW_SAMPLES).astype(np.float32)
        sr = np.array(_MODEL_SAMPLE_RATE, dtype=np.int64)

        if self._uses_unified_state:
            feeds = {"input": x, "state": self._state, "sr": sr}
            out, new_state = self._session.run(None, feeds)  # type: ignore[union-attr]
            self._state = np.asarray(new_state, dtype=np.float32)
        else:
            feeds = {"input": x, "h": self._h, "c": self._c, "sr": sr}
            out, new_h, new_c = self._session.run(None, feeds)  # type: ignore[union-attr]
            self._h = np.asarray(new_h, dtype=np.float32)
            self._c = np.asarray(new_c, dtype=np.float32)
        return float(np.asarray(out).ravel()[0])

    def _to_model_rate(self, samples: np.ndarray) -> np.ndarray:
        """Resample to 16 kHz via linear interpolation (no scipy dependency)."""
        if self.sample_rate == _MODEL_SAMPLE_RATE or samples.size == 0:
            return samples
        n_out = int(round(samples.shape[0] * _MODEL_SAMPLE_RATE / self.sample_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        src_idx = np.linspace(0.0, samples.shape[0] - 1, num=n_out, dtype=np.float64)
        return np.interp(
            src_idx, np.arange(samples.shape[0], dtype=np.float64), samples
        ).astype(np.float32)


if __name__ == "__main__":
    # Smoke test: synthetic noise vs silence. Skips cleanly when the model is
    # absent (no network in CI) — import and construction must always work.
    rng = np.random.default_rng(0)
    sr = _MODEL_SAMPLE_RATE
    frame_n = sr * 30 // 1000  # 30 ms frames, matching the default mic frame
    vad = SileroVAD(sample_rate=sr, frame_samples=frame_n, threshold=0.5)

    try:
        loud = (0.3 * rng.standard_normal(frame_n * 20)).astype(np.float32)
        quiet = np.zeros(frame_n * 20, dtype=np.float32)
        noise_hits = sum(
            vad.is_speech(loud[i : i + frame_n])
            for i in range(0, loud.size - frame_n, frame_n)
        )
        vad.reset()
        silence_hits = sum(
            vad.is_speech(quiet[i : i + frame_n])
            for i in range(0, quiet.size - frame_n, frame_n)
        )
        print(f"silence speech-frames: {silence_hits} (expect ~0)")
        print(f"noise   speech-frames: {noise_hits}")
        print("OK: model ran on synthetic audio.")
    except RuntimeError as exc:
        print(f"SKIP (model/onnxruntime not present): {exc}")

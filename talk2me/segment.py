"""Turn segmentation: a stream of mic frames -> discrete utterances.

Watches VAD output and emits one float32 utterance buffer per turn. A turn ends
when trailing silence exceeds `silence_ms`, provided we heard at least
`min_speech_ms` of speech (so coughs and key-clicks don't fire a turn).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np

from .config import Config
from .protocols import VAD


async def segment_utterances(
    frames: AsyncIterator[np.ndarray],
    vad: VAD,
    cfg: Config,
) -> AsyncIterator[np.ndarray]:
    """Yield one concatenated float32 utterance per detected turn.

    `frames` yields fixed-size float32 mono blocks (len == vad.frame_samples).
    """
    frame_ms = (vad.frame_samples / vad.sample_rate) * 1000.0
    silence_frames_needed = max(1, int(cfg.silence_ms / frame_ms))
    min_speech_frames = max(1, int(cfg.min_speech_ms / frame_ms))
    # Force-emit ceiling (frames of buffered audio). Guards against a stuck-open
    # VAD growing `buf` without bound and never ending a turn. 0 disables.
    max_buf_frames = (
        max(1, int(cfg.max_utterance_ms / frame_ms)) if cfg.max_utterance_ms > 0 else 0
    )

    buf: list[np.ndarray] = []
    speech_frames = 0
    trailing_silence = 0
    in_speech = False

    async for frame in frames:
        speech = vad.is_speech(frame)

        if speech:
            if not in_speech:
                in_speech = True
                vad.reset()
                if cfg.debug:
                    print("  ▶ speech", flush=True)
            buf.append(frame)
            speech_frames += 1
            trailing_silence = 0
            if max_buf_frames and len(buf) >= max_buf_frames:
                # Ceiling hit mid-speech: emit what we have so the loop makes
                # progress (and memory stays bounded) instead of buffering forever.
                if cfg.debug:
                    print(
                        f"  ⏹ max utterance (~{int(len(buf) * frame_ms)}ms) -> "
                        "force-emitting",
                        flush=True,
                    )
                yield np.concatenate(buf).astype(np.float32)
                buf = []
                speech_frames = 0
                trailing_silence = 0
                in_speech = False
                vad.reset()
            continue

        if in_speech:
            # Keep trailing silence in the buffer so whisper has natural padding.
            buf.append(frame)
            trailing_silence += 1
            if trailing_silence >= silence_frames_needed:
                emitted = speech_frames >= min_speech_frames
                if cfg.debug:
                    dur_ms = int(speech_frames * frame_ms)
                    print(
                        f"  ⏹ turn end: ~{dur_ms}ms speech -> "
                        f"{'transcribing' if emitted else 'ignored (too short)'}",
                        flush=True,
                    )
                if emitted:
                    yield np.concatenate(buf).astype(np.float32)
                # Reset for the next turn regardless of whether we emitted.
                buf = []
                speech_frames = 0
                trailing_silence = 0
                in_speech = False
                vad.reset()

    # Stream ended (finite source, or mic stopped mid-utterance). If we were
    # still mid-speech and it qualified, the silence-terminated path above never
    # fired — flush the buffered final utterance so it isn't silently lost.
    if in_speech and speech_frames >= min_speech_frames:
        if cfg.debug:
            dur_ms = int(speech_frames * frame_ms)
            print(
                f"  ⏹ stream end: ~{dur_ms}ms speech -> transcribing (flush)",
                flush=True,
            )
        yield np.concatenate(buf).astype(np.float32)

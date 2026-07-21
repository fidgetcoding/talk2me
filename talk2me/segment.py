"""Turn segmentation: a stream of mic frames -> discrete utterances.

Watches VAD output and emits one float32 utterance buffer per turn. A turn ends
when trailing silence exceeds `silence_ms`, provided we heard at least
`min_speech_ms` of speech in total AND one sustained voiced run of at least
`min_speech_run_ms`. The run requirement is what rejects taps, clicks, and
key presses: each is a 30-90ms transient, so no matter how many accumulate
toward the cumulative bar, none ever sustains like a spoken word does.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator

import numpy as np

from .config import Config
from .protocols import VAD

# See the short-utterance close in segment_utterances: speech totals at or
# under _SHORT_UTTERANCE_MS end their turn after _SHORT_SILENCE_MS of
# trailing silence instead of the full cfg.silence_ms.
_SHORT_UTTERANCE_MS = 1500
_SHORT_SILENCE_MS = 800


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
    # Short utterances close on a tighter window: sub-1.5s speech is
    # command-shaped ("pause", "wake up", "stop") and waiting the full
    # silence_ms reads as lag (live complaint 2026-07-20). Longer speech
    # keeps the full window — mid-thought pauses are real; that's why
    # silence_ms went 900->1200 in the first place. The rare short opener
    # of a longer sentence this closes early is caught by pre-send
    # stitching, which holds unfinished-sounding turns and joins.
    short_silence_frames = max(
        1, int(min(cfg.silence_ms, _SHORT_SILENCE_MS) / frame_ms)
    )
    short_speech_frames = int(_SHORT_UTTERANCE_MS / frame_ms)
    min_speech_frames = max(1, int(cfg.min_speech_ms / frame_ms))
    min_run_frames = max(
        1, int(getattr(cfg, "min_speech_run_ms", 0) / frame_ms)
    )
    # Force-emit ceiling (frames of buffered audio). Guards against a stuck-open
    # VAD growing `buf` without bound and never ending a turn. 0 disables.
    max_buf_frames = (
        max(1, int(cfg.max_utterance_ms / frame_ms)) if cfg.max_utterance_ms > 0 else 0
    )

    # Rolling window of the frames just BEFORE onset, prepended to the
    # utterance. Energy VADs trigger a beat late on quiet first phonemes;
    # without this the transcript starts mid-word ("count down…" -> "down…").
    pre_roll_frames = (
        max(1, int(cfg.pre_roll_ms / frame_ms)) if cfg.pre_roll_ms > 0 else 0
    )
    preroll: deque[np.ndarray] = deque(maxlen=pre_roll_frames or 1)

    buf: list[np.ndarray] = []
    speech_frames = 0
    run_frames = 0  # current consecutive voiced run
    longest_run = 0  # best run this turn — must clear min_run_frames to emit
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
                if pre_roll_frames and preroll:
                    buf.extend(preroll)
                    preroll.clear()
            buf.append(frame)
            speech_frames += 1
            run_frames += 1
            longest_run = max(longest_run, run_frames)
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
                run_frames = 0
                longest_run = 0
                trailing_silence = 0
                in_speech = False
                vad.reset()
            continue

        if not in_speech:
            # Idle silence: remember it for the pre-roll window.
            if pre_roll_frames:
                preroll.append(frame)
            continue

        if in_speech:
            # Keep trailing silence in the buffer so whisper has natural padding.
            buf.append(frame)
            run_frames = 0
            trailing_silence += 1
            needed = (
                short_silence_frames
                if speech_frames <= short_speech_frames
                else silence_frames_needed
            )
            if trailing_silence >= needed:
                enough = speech_frames >= min_speech_frames
                sustained = longest_run >= min_run_frames
                emitted = enough and sustained
                if cfg.debug:
                    dur_ms = int(speech_frames * frame_ms)
                    if emitted:
                        verdict = "transcribing"
                    elif not enough:
                        verdict = "ignored (too short)"
                    else:
                        verdict = (
                            "ignored (no sustained speech — taps/clicks, "
                            f"longest run ~{int(longest_run * frame_ms)}ms)"
                        )
                    print(f"  ⏹ turn end: ~{dur_ms}ms speech -> {verdict}", flush=True)
                if emitted:
                    yield np.concatenate(buf).astype(np.float32)
                # Reset for the next turn regardless of whether we emitted.
                buf = []
                speech_frames = 0
                run_frames = 0
                longest_run = 0
                trailing_silence = 0
                in_speech = False
                vad.reset()

    # Stream ended (finite source, or mic stopped mid-utterance). If we were
    # still mid-speech and it qualified, the silence-terminated path above never
    # fired — flush the buffered final utterance so it isn't silently lost.
    if in_speech and speech_frames >= min_speech_frames and longest_run >= min_run_frames:
        if cfg.debug:
            dur_ms = int(speech_frames * frame_ms)
            print(
                f"  ⏹ stream end: ~{dur_ms}ms speech -> transcribing (flush)",
                flush=True,
            )
        yield np.concatenate(buf).astype(np.float32)

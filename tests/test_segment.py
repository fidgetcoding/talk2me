"""Pure-logic test for the turn segmenter. Instant, no audio, no model.

Builds a synthetic frame stream (loud frames = speech, zero frames = silence)
and asserts the segmenter emits exactly one utterance with the right shape.

Run:  ./.venv/bin/python -m tests.test_segment
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.segment import segment_utterances
from talk2me.vad import EnergyVAD

SR = 16000
FRAME = 480  # 30 ms


async def _frames(seq):
    for f in seq:
        yield f


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


async def _segment(stream, cfg):
    vad = EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012)
    return [u async for u in segment_utterances(_frames(stream), vad, cfg)]


async def main() -> int:
    cfg = Config(silence_ms=900, min_speech_ms=250)  # 30 silence / ~9 speech frames

    # --- Case 1: normal silence-terminated turn (existing behavior) ---
    # 1 real utterance: 15 speech + 35 silence. Then a sub-min blip: 3 speech.
    stream = _speech(15) + _silence(35) + _speech(3) + _silence(35)
    got = await _segment(stream, cfg)

    one_utterance = len(got) == 1
    # buffer = 15 speech + 30 silence frames (fires at threshold) = 45 * 480
    right_len = one_utterance and got[0].shape[0] == 45 * FRAME
    blip_ignored = one_utterance  # the 3-frame blip never crossed min_speech
    case1 = one_utterance and right_len and blip_ignored
    print(f"[case1 silence-terminated] utterances={len(got)} "
          f"len={(got[0].shape[0] if got else 0)} expected={45*FRAME} "
          f"-> {'PASS' if case1 else 'FAIL'}")

    # --- Case 2: stream ends mid-speech, no trailing silence (I5 fix) ---
    # 15 speech frames, nothing after — must flush exactly one utterance.
    eos_stream = _speech(15)
    eos_got = await _segment(eos_stream, cfg)
    eos_one = len(eos_got) == 1
    eos_len = eos_one and eos_got[0].shape[0] == 15 * FRAME  # no silence padding
    case2 = eos_one and eos_len
    print(f"[case2 stream-end mid-speech] utterances={len(eos_got)} "
          f"len={(eos_got[0].shape[0] if eos_got else 0)} expected={15*FRAME} "
          f"-> {'PASS' if case2 else 'FAIL'}")

    # --- Case 3: stream ends with too-few speech frames (still ignored) ---
    # 3 speech frames < min_speech (~9) — must NOT emit even on stream end.
    short_stream = _speech(3)
    short_got = await _segment(short_stream, cfg)
    case3 = len(short_got) == 0
    print(f"[case3 stream-end too-short] utterances={len(short_got)} "
          f"expected=0 -> {'PASS' if case3 else 'FAIL'}")

    # --- Case 4: stuck-open VAD hits the max-utterance ceiling ---
    # 50 continuous speech frames with a 600ms cap (20 frames @ 30ms) must
    # force-emit bounded utterances instead of buffering forever: two full
    # 20-frame emissions + a 10-frame tail flushed at stream end.
    cap_cfg = Config(silence_ms=900, min_speech_ms=250, max_utterance_ms=600)
    cap_got = await _segment(_speech(50), cap_cfg)
    cap_lens = [u.shape[0] for u in cap_got]
    case4 = cap_lens == [20 * FRAME, 20 * FRAME, 10 * FRAME]
    print(f"[case4 max-utterance force-emit] lens={cap_lens} "
          f"expected={[20*FRAME, 20*FRAME, 10*FRAME]} -> "
          f"{'PASS' if case4 else 'FAIL'}")

    # --- Case 5: pre-roll — leading quiet audio is prepended on onset ---
    # 5 idle silence frames, then speech. With pre_roll_ms=300 (10 frames) the
    # emitted utterance must include the 5 buffered pre-onset frames, so the
    # first word's quiet opening phoneme isn't clipped.
    pre_stream = _silence(5) + _speech(15) + _silence(35)
    pre_got = await _segment(pre_stream, cfg)
    pre_one = len(pre_got) == 1
    pre_len = pre_one and pre_got[0].shape[0] == (5 + 15 + 30) * FRAME
    case5 = pre_one and pre_len
    print(f"[case5 pre-roll prepended] utterances={len(pre_got)} "
          f"len={(pre_got[0].shape[0] if pre_got else 0)} expected={50*FRAME} "
          f"-> {'PASS' if case5 else 'FAIL'}")

    # --- Case 6: pre-roll disabled -> old exact behavior ---
    no_pre_cfg = Config(silence_ms=900, min_speech_ms=250, pre_roll_ms=0)
    np_got = await _segment(_silence(5) + _speech(15) + _silence(35), no_pre_cfg)
    case6 = len(np_got) == 1 and np_got[0].shape[0] == 45 * FRAME
    print(f"[case6 pre-roll disabled] len="
          f"{(np_got[0].shape[0] if np_got else 0)} expected={45*FRAME} "
          f"-> {'PASS' if case6 else 'FAIL'}")

    ok = case1 and case2 and case3 and case4 and case5 and case6
    print(f"-> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

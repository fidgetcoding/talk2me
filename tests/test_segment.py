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


async def main() -> int:
    cfg = Config(silence_ms=900, min_speech_ms=250)  # 30 silence / ~9 speech frames
    vad = EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012)

    # 1 real utterance: 15 speech + 35 silence. Then a sub-min blip: 3 speech.
    stream = _speech(15) + _silence(35) + _speech(3) + _silence(35)
    got = [u async for u in segment_utterances(_frames(stream), vad, cfg)]

    one_utterance = len(got) == 1
    # buffer = 15 speech + 30 silence frames (fires at threshold) = 45 * 480
    right_len = one_utterance and got[0].shape[0] == 45 * FRAME
    blip_ignored = one_utterance  # the 3-frame blip never crossed min_speech

    ok = one_utterance and right_len and blip_ignored
    print(f"utterances={len(got)} "
          f"len={(got[0].shape[0] if got else 0)} expected={45*FRAME} "
          f"-> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

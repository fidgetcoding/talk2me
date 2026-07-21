"""Headless tests for the native-AEC capture path (talk2me/vpio.py).

Real echo cancellation is untestable without hardware — that's Phase 3's
field protocol. What IS testable headless, and what these lock down:

- the resamplers (FIR decimator + linear fallback) are stream-stateful:
  feeding one big array or many ragged chunks yields identical output;
- decimation preserves in-band content and the output rate math;
- VoiceProcessingMic's framing/mute/queue semantics match audio.Mic's
  contract exactly (the orchestrator must not be able to tell them apart).

Run:  ./.venv/bin/python -m tests.test_vpio
"""

import asyncio

import numpy as np

from talk2me.vpio import (
    VoiceProcessingMic,
    _FirDecimator,
    _LinearResampler,
    is_available,
)

FAILS = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILS
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS += 1


def _sine(freq: float, rate: int, seconds: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def _crossings(x: np.ndarray) -> int:
    return int(np.sum(np.abs(np.diff(np.signbit(x.astype(np.float64))))))


def test_fir_decimator() -> None:
    x = _sine(1000, 48000, 1.0)

    one_shot = _FirDecimator(3).feed(x)
    check(
        "fir 3:1 output length",
        abs(one_shot.shape[0] - 16000) <= 1,
        f"got {one_shot.shape[0]}",
    )

    # ~1kHz should survive: zero-crossing count ≈ 2*freq*seconds.
    zc = _crossings(one_shot[100:])  # skip filter warm-in
    check("fir preserves 1kHz tone", abs(zc - 2000) < 60, f"crossings {zc}")

    # Stream-state continuity: ragged chunks == one shot, bit-exact-ish.
    dec = _FirDecimator(3)
    outs = []
    i = 0
    for size in [1, 7, 480, 4096, 33, 100000]:
        outs.append(dec.feed(x[i : i + size]))
        i += size
    outs.append(dec.feed(x[i:]))
    chunked = np.concatenate(outs)
    n = min(chunked.shape[0], one_shot.shape[0])
    check(
        "fir chunked == one-shot",
        n > 15000 and bool(np.allclose(chunked[:n], one_shot[:n], atol=1e-6)),
        f"compared {n}",
    )

    # Ratio 1 = passthrough, no copy games.
    same = _FirDecimator(1).feed(x)
    check("fir ratio-1 passthrough", same is x)

    # Above-Nyquist content must die (aliasing guard): 10kHz at 48k is well
    # above the 16k output's 8k Nyquist — post-decimation it should be gone.
    hiss = _sine(10000, 48000, 0.5)
    out = _FirDecimator(3).feed(hiss)
    rms = float(np.sqrt(np.mean(out[200:] ** 2)))
    check("fir kills above-Nyquist", rms < 0.02, f"residual rms {rms:.4f}")


def test_linear_resampler() -> None:
    x = _sine(440, 44100, 1.0)
    rs = _LinearResampler(44100, 16000)
    outs = []
    i = 0
    for size in [3, 441, 1000, 4410, 12345]:
        outs.append(rs.feed(x[i : i + size]))
        i += size
    outs.append(rs.feed(x[i:]))
    y = np.concatenate(outs)
    check(
        "linear 44.1k->16k length",
        abs(y.shape[0] - 16000) <= 3,
        f"got {y.shape[0]}",
    )
    zc = _crossings(y[50:])
    check("linear preserves 440Hz", abs(zc - 880) < 40, f"crossings {zc}")

    dc = np.ones(44100, dtype=np.float32) * 0.5
    rs2 = _LinearResampler(44100, 16000)
    ydc = np.concatenate([rs2.feed(dc[:100]), rs2.feed(dc[100:])])
    check(
        "linear DC stays DC",
        bool(np.allclose(ydc, 0.5, atol=1e-6)),
        f"minmax {ydc.min():.4f}/{ydc.max():.4f}",
    )


async def test_mic_framing() -> None:
    mic = VoiceProcessingMic(16000, 480)
    mic._loop = asyncio.get_running_loop()
    mic._resampler = None  # framing under test, not resampling

    # Ragged chunks -> exact 480-sample frames, remainder held as pending.
    mic._ingest(np.ones(100, dtype=np.float32))
    mic._ingest(np.ones(500, dtype=np.float32))
    await asyncio.sleep(0)
    check("frame after 600 samples", mic._queue.qsize() == 1)
    frame = mic._queue.get_nowait()
    check("frame length exact", frame.shape[0] == 480)
    check("pending holds remainder", mic._pending.shape[0] == 120)

    # One big chunk -> many frames.
    mic._ingest(np.ones(480 * 3, dtype=np.float32))
    await asyncio.sleep(0)
    check("multi-frame chunk", mic._queue.qsize() == 3)
    while not mic._queue.empty():
        mic._queue.get_nowait()

    # Mute discards at source AND clears pending (no replay on unmute).
    mic._ingest(np.ones(200, dtype=np.float32))
    mic.set_muted(True)
    mic._ingest(np.ones(480 * 4, dtype=np.float32))
    await asyncio.sleep(0)
    check("muted delivers nothing", mic._queue.qsize() == 0)
    check("mute cleared pending", mic._pending.shape[0] == 0)
    mic.set_muted(False)
    mic._ingest(np.ones(480, dtype=np.float32))
    await asyncio.sleep(0)
    check("unmute resumes cleanly", mic._queue.qsize() == 1)
    mic._queue.get_nowait()

    # Drop-oldest under backlog — inherited Mic semantics.
    first = np.zeros(480, dtype=np.float32)
    mic._ingest(first)
    for _ in range(120):  # queue maxsize is 100
        mic._ingest(np.ones(480, dtype=np.float32))
    await asyncio.sleep(0)
    check("queue bounded", mic._queue.qsize() <= 100)
    check("drop-oldest counted", mic.dropped_frames > 0)
    oldest = mic._queue.get_nowait()
    check("oldest frame evicted first", float(oldest.max()) == 1.0)

    # switch_device records but never touches an engine.
    mic.switch_device(7)
    check("switch_device is inert", mic.device == 7 and mic._engine is None)


def test_availability() -> None:
    # On the dev Mac this is True; on CI/Linux it must be a clean False, not
    # an exception. Either way it must return a bool.
    avail = is_available()
    check("is_available returns bool", isinstance(avail, bool), f"got {avail}")


async def main() -> int:
    test_fir_decimator()
    test_linear_resampler()
    await test_mic_framing()
    test_availability()
    print(f"\n{'ALL PASS' if FAILS == 0 else f'{FAILS} FAILURES'}")
    return FAILS


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

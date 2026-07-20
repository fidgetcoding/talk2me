"""Speaker barge-in race, headless: stop() during an in-flight write must NOT
crash the loop.

Reproduces the live-run failure (PortAudio -9986): the barge-in monitor calls
Speaker.stop() while play() has a blocking stream.write on a worker thread.
The old stop() closed the stream under the write; the fix aborts instead and
play() treats the resulting PortAudioError as a cancellation.

Uses a stub sounddevice module so no audio hardware is touched.

Run:  ./.venv/bin/python -m tests.test_speaker_stop
"""

import asyncio
import time

import numpy as np

from talk2me import audio

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


class StubPortAudioError(Exception):
    pass


class StubOutputStream:
    def __init__(self, **kwargs) -> None:
        self.aborted = False
        self.closed = False
        self.writes = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True

    def write(self, data) -> None:
        # Emulate a blocking device write (runs on the to_thread worker).
        time.sleep(0.05)
        self.writes += 1
        if self.aborted or self.closed:
            raise StubPortAudioError("write to aborted/closed stream")


class StubSD:
    PortAudioError = StubPortAudioError
    OutputStream = StubOutputStream


async def _blocks(n: int):
    for _ in range(n):
        yield np.zeros(256, dtype=np.float32)


async def main() -> int:
    real_sd = audio.sd
    audio.sd = StubSD
    try:
        # --- stop() mid-write: no exception, play returns False, device freed ---
        sp = audio.Speaker(16000)

        async def stop_soon():
            await asyncio.sleep(0.01)  # land inside the first blocking write
            sp.stop()

        stopper = asyncio.create_task(stop_soon())
        try:
            result = await asyncio.wait_for(sp.play(_blocks(10)), timeout=5)
            crashed = False
        except Exception as exc:  # the live-run failure mode
            result, crashed = None, True
            print(f"  play() raised: {exc!r}", flush=True)
        await stopper
        check("stop mid-write does not crash", not crashed)
        check("interrupted play returns False", result is False, f"result={result}")
        check("stream released after interrupt", sp._stream is None)

        # --- normal completion still works on a fresh speaker ---
        sp2 = audio.Speaker(16000)
        ok = await asyncio.wait_for(sp2.play(_blocks(3)), timeout=5)
        check("uninterrupted play returns True", ok is True, f"result={ok}")

        # --- idle stop() still tears the stream down (turn-end contract) ---
        sp2.stop()
        check("idle stop releases stream", sp2._stream is None)
    finally:
        audio.sd = real_sd

    passed = sum(1 for _, r in RESULTS if r)
    all_ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if all_ok else 'FAIL'}", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

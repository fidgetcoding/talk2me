"""The render and play workers: order, overlap, merging. No audio, no `say`.

A fake voice with a real delay stands in for `say`, because the point is timing:
renders have to overlap, and the voice still has to come out in order.

Run:  python -m tests.test_speech_pipeline
"""

import asyncio
import time

import numpy as np

from talk2me.config import Config
from talk2me.orchestrator import Orchestrator
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


class SlowVoice:
    sample_rate = 16000

    def __init__(self, *, merge: bool, delay: float = 0.05, fail_on: str | None = None) -> None:
        self.merge_queued_text = merge
        self.delay = delay
        self.fail_on = fail_on
        self.rendered: list[str] = []
        self.active = 0
        self.max_active = 0

    async def synthesize(self, text):
        self.rendered.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail_on and self.fail_on in text:
                raise RuntimeError("render failed")
        finally:
            self.active -= 1
        # Tag each chunk with its render index, so playback order is checkable.
        yield np.full(160, float(self.rendered.index(text)), dtype=np.float32)


class LabelSpeaker(FakeSpeaker):
    def __init__(self) -> None:
        super().__init__(16000)
        self.heard: list[int] = []

    async def play(self, blocks) -> bool:
        async for block in blocks:
            self.heard.append(int(block[0]))
        return True


def _orchestrator(tts, speaker) -> Orchestrator:
    return Orchestrator(
        cfg=Config(),
        backend=FakeBackend(),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=FakeSTT([]),
        tts=tts,
        mic=FakeMic([], sample_rate=16000),
        speaker=speaker,
    )


async def _speak(orch: Orchestrator, sentences: list[str], *, after_first: float = 0.0) -> None:
    orch._start_speech_pipeline()
    await orch._render_q.put(sentences[0])
    if after_first:
        await asyncio.sleep(after_first)
    for sentence in sentences[1:]:
        await orch._render_q.put(sentence)
    await orch._drain_speech_pipeline()


async def test_overlap_and_order() -> None:
    tts = SlowVoice(merge=False)
    speaker = LabelSpeaker()
    start = time.monotonic()
    await _speak(_orchestrator(tts, speaker), ["One.", "Two.", "Three."])
    check("renders overlap", tts.max_active >= 2, f"max at once {tts.max_active}")
    check("still plays in order", speaker.heard == [0, 1, 2], str(speaker.heard))
    check("no merging unless the engine asks", tts.rendered == ["One.", "Two.", "Three."])
    check("overlap actually saves time", time.monotonic() - start < 0.14)


async def test_merging() -> None:
    tts = SlowVoice(merge=True)
    speaker = LabelSpeaker()
    # The first sentence is alone in the queue. The rest land while it renders.
    await _speak(_orchestrator(tts, speaker), ["One.", "Two.", "Three."], after_first=0.01)
    check("waiting sentences merge", tts.rendered == ["One.", "Two. Three."], str(tts.rendered))
    check("merged chunks play in order", speaker.heard == [0, 1], str(speaker.heard))

    tts = SlowVoice(merge=True)
    long = "x" * 130 + "."
    await _speak(_orchestrator(tts, LabelSpeaker()), ["One.", long, "After the cap."], after_first=0.01)
    check("merging stops at the cap", tts.rendered == ["One.", long, "After the cap."], str([t[:12] for t in tts.rendered]))
    check("no text lost", "After the cap." in " ".join(tts.rendered))


async def test_end_marker_while_merging() -> None:
    tts = SlowVoice(merge=True)
    speaker = LabelSpeaker()
    orch = _orchestrator(tts, speaker)
    orch._start_speech_pipeline()
    # Everything, end marker included, is queued before the worker wakes up.
    for item in ("One.", "Two.", None):
        await orch._render_q.put(item)
    await asyncio.wait_for(asyncio.gather(orch._render_task, orch._play_task), 2)
    check("end marker swallowed by a merge still ends speech", tts.rendered == ["One. Two."], str(tts.rendered))
    check("that last chunk still plays", speaker.heard == [0], str(speaker.heard))


async def test_bad_render_skips() -> None:
    tts = SlowVoice(merge=False, fail_on="Two")
    speaker = LabelSpeaker()
    await _speak(_orchestrator(tts, speaker), ["One.", "Two.", "Three."])
    check("a failed render skips its chunk, the rest play", speaker.heard == [0, 2], str(speaker.heard))


async def main_async() -> None:
    await test_overlap_and_order()
    await test_merging()
    await test_end_marker_while_merging()
    await test_bad_render_skips()


def main() -> int:
    asyncio.run(main_async())
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

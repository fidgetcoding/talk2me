"""Does a session that runs all day keep growing? Fakes and pipes, no audio, no model.

Python-level memory is tracked with tracemalloc, counted only in talk2me's own files,
so the fakes' bookkeeping doesn't muddy it. The real-process check watches the whole
process through real pipes, with a stand-in agent.

Run:  python -m tests.test_long_session
"""

import asyncio
import contextlib
import gc
import os
import subprocess
import sys
import tempfile
import tracemalloc
from pathlib import Path

import numpy as np
from rich.console import Console

from talk2me.config import Config
from talk2me.echogate import EchoRef
from talk2me.events import AssistantTextDelta, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.render import PlainRenderer
from talk2me.retro import RetroRenderer
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeTTS

RESULTS: list[bool] = []
TURNS = 1500
SPEECH = (np.random.randn(480) * 0.2).astype(np.float32)
SILENCE = np.zeros(480, dtype=np.float32)
FAKE = Path(__file__).with_name("fake_claude_cli.py")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    RESULTS.append(bool(ok))


def _ours() -> int:
    gc.collect()
    snapshot = tracemalloc.take_snapshot().filter_traces(
        [tracemalloc.Filter(True, f"*{os.sep}talk2me{os.sep}talk2me{os.sep}*"),
         tracemalloc.Filter(True, f"*{os.sep}talk2me-public{os.sep}talk2me{os.sep}*")]
    )
    return sum(s.size for s in snapshot.statistics("filename"))


class EndlessMic(FakeMic):
    """Yields turn after turn without keeping any of it."""

    def __init__(self, pattern: list[np.ndarray], repeats: int) -> None:
        super().__init__([])
        self.pattern, self.repeats = pattern, repeats

    async def frames(self):
        for _ in range(self.repeats):
            for frame in self.pattern:
                await asyncio.sleep(0)
                if not self.muted:
                    yield frame


class ForgetfulTTS(FakeTTS):
    async def synthesize(self, text):
        yield np.zeros(256, dtype=np.float32)


class ForgetfulBackend(FakeBackend):
    """Replies forever, keeping only a count, and samples memory along the way."""

    def __init__(self, marks: dict[int, int]) -> None:
        super().__init__()
        self.turns = 0
        self.marks = marks

    async def send(self, user_text: str) -> None:
        self.turns += 1
        if self.turns in (TURNS // 5, TURNS - 1):
            self.marks[self.turns] = _ours()
        reply = "It's in useAuthStore and sync_helpers.py. Want me to fix it? I can run the tests too."
        await self._q.put(AssistantTextDelta(text=reply))
        await self._q.put(TurnComplete(text=reply))


class Ears:
    def __init__(self) -> None:
        self.n = 0

    async def transcribe(self, audio, sample_rate):
        self.n += 1
        return f"Please look at turn number {self.n} for me."


async def _long_run(renderer) -> dict[int, int]:
    marks: dict[int, int] = {}
    pattern = [SPEECH] * 8 + [SILENCE] * 14
    orch = Orchestrator(
        cfg=Config(silence_ms=300, min_speech_ms=100),
        backend=ForgetfulBackend(marks),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=Ears(), tts=ForgetfulTTS(), mic=EndlessMic(pattern, TURNS), speaker=FakeSpeaker(16000),
        renderer=renderer,
    )
    await asyncio.wait_for(orch.run(), 300)
    return marks


async def test_many_turns() -> None:
    # devnull, not a StringIO: a buffer holding all the output would read as a leak.
    sink = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115, ASYNC230
    for label, make in (("plain", PlainRenderer), ("retro", lambda: RetroRenderer(Console(file=sink, force_terminal=True, width=100)))):
        tracemalloc.start()
        with contextlib.redirect_stdout(sink):
            marks = await _long_run(make())
        tracemalloc.stop()
        early, late = marks[TURNS // 5], marks[TURNS - 1]
        growth = (late - early) / 1024
        check(f"{TURNS} turns, {label} screen: talk2me's own memory stays flat", growth < 128, f"{early // 1024} KB -> {late // 1024} KB")


async def test_idle_hours() -> None:
    """Nobody talking for 20 minutes of frames: no buffers should build."""
    tracemalloc.start()
    marks = {}
    orch = Orchestrator(
        cfg=Config(), backend=FakeBackend(),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=Ears(), tts=ForgetfulTTS(), mic=EndlessMic([SILENCE] * 2000, 20), speaker=FakeSpeaker(16000),
    )
    with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):  # noqa: ASYNC230
        await asyncio.wait_for(orch.run(), 120)
    marks["end"] = _ours()
    tracemalloc.stop()
    check("20 minutes of silence builds nothing", marks["end"] / 1024 < 2048, f"{marks['end'] // 1024} KB")


def test_echo_reference_bounded() -> None:
    ref = EchoRef(16000)
    second = np.zeros(16000, dtype=np.float32)
    for _ in range(3600):
        ref.add(second)
    held = sum(getattr(v, "nbytes", 0) for v in vars(ref).values())
    check("an hour of playback doesn't pile up in the echo reference", held < 16 * 1024 * 1024, f"{held // 1024} KB")


def _rss_kb(pid: int) -> int:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return int(out.stdout.strip() or 0)


def test_real_process() -> None:
    if sys.platform == "win32":
        return
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        claude = folder / "claude"
        claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        claude.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "TALK2ME_SAVE_DIR"}
        env.update(PATH=f"{folder}{os.pathsep}{env['PATH']}", 
                   TALK2ME_CONFIG=str(folder / "none.json"), TALK2ME_STATE=str(folder / "state.json"), TALK2ME_SAVE_DIR=str(folder / "logs"))
        env.pop("FAKE_CLAUDE_LOG", None)
        proc = subprocess.Popen([sys.executable, "-m", "talk2me", "--text", "--cwd", str(folder)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        marks = {}
        for i in range(1, 5001):
            proc.stdin.write(b"hello there\n")
            proc.stdin.flush()
            if i in (1000, 5000):
                # Backpressure-free pacing: let it catch up before measuring.
                subprocess.run(["sleep", "3"], check=False)
                marks[i] = _rss_kb(proc.pid)
        proc.stdin.close()
        proc.wait(60)
        grew = marks[5000] - marks[1000]
        check("5,000 typed turns through real pipes: the process stays flat", grew < 20 * 1024, f"{marks[1000]} KB -> {marks[5000]} KB")
        check("and exits cleanly", proc.returncode == 0, f"exit {proc.returncode}")


def main() -> int:
    asyncio.run(test_many_turns())
    asyncio.run(test_idle_hours())
    test_echo_reference_bounded()
    test_real_process()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

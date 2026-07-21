"""Manual check: native-AEC speakers self-cut protocol, no human required.

Field-protocol tests 1-2 in a bottle: the agent speaks a LONG counting
answer through the REAL speakers while the REAL VoiceProcessingMic is hot
and the barge monitor is armed — exactly the configuration that produced
self-barges in v2.4. Everything is real (VPIO capture, PortAudio Speaker,
`say` TTS, silero speech-check, whisper for any barge transcription) except
the brain, which is a scripted FakeBackend so the run is deterministic and
free. A self-cut shows up as backend.interrupts > 0.

The one thing this cannot test is a human talking over it — native AEC
cancels ALL system output, so any speaker-played "voice" is invisible by
design. That half of the protocol needs a person (docs/v2.5-aec-plan.md
Phase 3).

Not in CI (needs macOS, speakers, quiet room; PLAYS AUDIO OUT LOUD).

Run:  ./.venv/bin/python -m tests.manual_vpio_selfcut_check [volume 0-100]...
      (default: one pass at 45, one at 100; restores the original volume)
"""

import asyncio
import subprocess
import sys

from talk2me import factory
from talk2me.audio import Speaker
from talk2me.config import Config
from talk2me.orchestrator import Orchestrator
from talk2me.speechcheck import build_speech_check
from talk2me.tts import SayTTS
from talk2me.vpio import VoiceProcessingMic, probe

from .fakes import FakeBackend

COUNT_REPLY = (
    "Counting for the echo test now. "
    + " ".join(f"{w}." for w in (
        "One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve "
        "Thirteen Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen "
        "Twenty".split()
    ))
    + " That is the whole count, done."
)

# Dynamic prose is what actually false-cut v2.4 live (monotone counts always
# passed the residual gate; sentence-shaped prose did not) — so the protocol
# runs BOTH shapes.
PROSE_REPLY = (
    "A car engine works by burning a mixture of fuel and air inside small "
    "chambers called cylinders. Each tiny explosion pushes a piston down, "
    "and the pistons turn a crankshaft, which ultimately spins the wheels. "
    "The battery starts the process, the alternator keeps everything "
    "charged, and the cooling system carries the heat away so the metal "
    "never melts. Four strokes repeat over and over: intake, compression, "
    "combustion, and exhaust. That is the whole story, more or less."
)


def _get_volume() -> int:
    out = subprocess.run(
        ["osascript", "-e", "output volume of (get volume settings)"],
        capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


def _set_volume(v: int) -> None:
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {v}"],
        capture_output=True, check=True,
    )


async def one_pass(volume: int, reply: str) -> tuple[int, bool]:
    """Speak `reply` at `volume`; return (interrupts, completed)."""
    _set_volume(volume)
    cfg = Config(
        barge_in=True,
        half_duplex=False,
        aec_active=True,
        aec_layer="native",
    )
    backend = FakeBackend(replies=[reply])
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=factory.build_vad(cfg),
        stt=factory.build_stt(cfg),
        tts=SayTTS(rate_wpm=236),
        mic=VoiceProcessingMic(cfg.sample_rate, factory.frame_samples(cfg)),
        speaker=Speaker(SayTTS.sample_rate),
        speech_check=build_speech_check(True),
    )
    run_task = asyncio.create_task(orch.run())
    await asyncio.sleep(6.0)  # warmup + SessionReady
    completed = False
    try:
        # Voice path (typed=True would mute the mic and void the test).
        await asyncio.wait_for(
            orch._handle_user_text("count for me", typed=False), timeout=120
        )
        completed = True
    except asyncio.TimeoutError:
        pass
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass
    return backend.interrupts, completed


async def main() -> int:
    if not probe():
        print("SKIP — voice-processing input didn't probe healthy")
        return 0
    volumes = [int(v) for v in sys.argv[1:]] or [45, 100]
    original = _get_volume()
    fails = 0
    try:
        for vol in volumes:
            for shape, reply in [("count", COUNT_REPLY), ("prose", PROSE_REPLY)]:
                interrupts, completed = await one_pass(vol, reply)
                ok = interrupts == 0 and completed
                fails += 0 if ok else 1
                print(
                    f"\n{'PASS' if ok else 'FAIL'}  volume {vol} {shape}: "
                    f"self-cuts={interrupts} completed={completed}"
                )
    finally:
        _set_volume(original)
        print(f"\n(volume restored to {original})")
    print("ALL PASS" if fails == 0 else f"{fails} FAILURES")
    return fails


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

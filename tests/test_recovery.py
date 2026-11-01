"""When a part breaks mid-session, you lose that part and not the conversation. Fakes
only: no audio, no model, no agent. Every failure here used to end or hang the session.

Run:  python -m tests.test_recovery
"""

import asyncio

import numpy as np

from talk2me.backends import ClaudeCodeBackend
from talk2me.config import Config
from talk2me.events import AssistantTextDelta, PermissionRequest, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


def _turns(n: int = 2) -> list[np.ndarray]:
    speech = [(np.random.randn(480) * 0.2).astype(np.float32) for _ in range(15)]
    silence = [np.zeros(480, dtype=np.float32) for _ in range(35)]
    return (speech + silence) * n


def _orchestrator(*, stt=None, tts=None, speaker=None, backend=None, frames=None, **kw) -> Orchestrator:
    orch = Orchestrator(
        cfg=Config(silence_ms=300, min_speech_ms=100),
        backend=backend or FakeBackend(["Fine. Really. All good. Yes. Done."] * 5),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=stt or FakeSTT(["First one.", "Second one."]),
        tts=tts or FakeTTS(),
        mic=FakeMic(frames or _turns()),
        speaker=speaker or FakeSpeaker(16000),
        **kw,
    )
    orch.errors = []
    orch.render.error = orch.errors.append
    return orch


async def _finishes(orch: Orchestrator) -> bool:
    try:
        await asyncio.wait_for(orch.run(), 15)
    except TimeoutError:
        return False
    return True


async def test_ears_fail_once() -> None:
    class Flaky(FakeSTT):
        async def transcribe(self, audio, sample_rate):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Metal ran out of memory")
            return "Second one."

    orch = _orchestrator(stt=Flaky([]))
    check("ears failing: session survives", await _finishes(orch))
    check("the next utterance still lands", orch.backend.sent == ["Second one."], str(orch.backend.sent))
    check("and it says what happened", any("[ears]" in e for e in orch.errors), str(orch.errors))


async def test_speaker_dies() -> None:
    class Dead(FakeSpeaker):
        async def play(self, blocks):
            raise OSError("device unplugged")

    tts = FakeTTS()
    orch = _orchestrator(speaker=Dead(16000), tts=tts)
    check("dead speaker: no hang", await _finishes(orch))
    check("the conversation carried on", len(orch.backend.sent) == 2, str(orch.backend.sent))
    faults = [e for e in orch.errors if "[speaker]" in e]
    check("says so once, not per chunk", len(faults) == 1, str(faults))


async def test_voice_breaks() -> None:
    class Broken(FakeTTS):
        async def synthesize(self, text):
            raise OSError("voice broke")
            yield

    orch = _orchestrator(tts=Broken())
    check("broken voice: session survives", await _finishes(orch) and len(orch.backend.sent) == 2)


async def test_speech_check_breaks() -> None:
    class Check:
        locked = False
        last_score = None

        def __call__(self, audio, sample_rate):
            raise RuntimeError("model file vanished")

    orch = _orchestrator(speech_check=Check())
    check("broken speech check: session survives", await _finishes(orch))
    check("audio passes instead of going deaf", orch.backend.sent == ["First one.", "Second one."], str(orch.backend.sent))


async def test_odd_permission_input() -> None:
    script = [PermissionRequest("r", "Bash", {}), AssistantTextDelta(text="Ok."), TurnComplete(text="Ok.")]
    orch = _orchestrator(backend=FakeBackend(scripts=[script]), stt=FakeSTT(["Do it.", "deny"]))
    check("permission with no input: still asks", await _finishes(orch))
    check("and answers", [r[1] for r in orch.backend.permission_responses] == [False])


def test_backend_translate_shapes() -> None:
    backend = ClaudeCodeBackend()
    req = {"type": "control_request", "request_id": "r", "request": {"subtype": "can_use_tool", "tool_name": "Bash\x1b[31m", "input": "a string"}}
    ev = backend._translate(req)[0]
    check("non-dict tool input becomes empty", ev.tool_input == {})
    check("tool name loses its escapes", "\x1b" not in ev.tool_name)
    req["request"]["input"] = {"command": "ls \x1b]52;c;aGk=\x07", "nested": ["\x1b[2J"]}
    ev = backend._translate(req)[0]
    check("escapes stripped through nested input", "\x1b" not in str(ev.tool_input) and "\x07" not in str(ev.tool_input))
    delta = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a\x1b[31mb\x00c"}}}
    check("agent text loses escapes and NUL", backend._translate(delta)[0].text == "a[31mbc")
    backend = ClaudeCodeBackend()
    number = {"type": "assistant", "message": {"content": [{"type": "text", "text": 5}]}}
    backend._translate(number)
    check("a number for text can't break the rollup", backend._translate({"type": "result"})[0].text == "5")


async def test_verdict_to_dead_agent() -> None:
    class Stdin:
        def write(self, _data):
            raise BrokenPipeError("agent gone")

        async def drain(self):
            pass

    backend = ClaudeCodeBackend()
    backend._proc = type("P", (), {"stdin": Stdin()})()
    await backend.respond_permission("r", True)
    check("answering a dead agent doesn't raise", True)


async def main_async() -> None:
    await test_ears_fail_once()
    await test_speaker_dies()
    await test_voice_breaks()
    await test_speech_check_breaks()
    await test_odd_permission_input()
    await test_verdict_to_dead_agent()


def main() -> int:
    test_backend_translate_shapes()
    asyncio.run(main_async())
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Echo gate: speakers full-duplex must cut on a real talk-over and must
NEVER cut (or self-converse) on its own playback echo. Headless — synthetic
audio, no devices, no LLM cost.

Run:  ./.venv/bin/python -m tests.test_echogate
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.echogate import EchoGate, EchoRef
from talk2me.events import AssistantTextDelta, TurnComplete
from talk2me.orchestrator import Orchestrator
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

SR = 16000
FRAME = 480

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def _voice(seconds: float, *, seed: int, mod_hz: float, phase: float = 0.0,
           amp: float = 0.2) -> np.ndarray:
    """Speech-shaped test signal: noise carrier under a syllabic envelope."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    t = np.arange(n) / SR
    envelope = 0.5 * (1.0 + np.sin(2 * np.pi * mod_hz * t + phase))
    return (rng.standard_normal(n) * envelope * amp).astype(np.float32)


def _as_echo(played: np.ndarray, *, delay_s: float = 0.12) -> np.ndarray:
    """What the mic hears of our own playback: delayed, attenuated, and run
    through the nonlinear compression of small laptop speakers."""
    delay = np.zeros(int(delay_s * SR), dtype=np.float32)
    distorted = np.tanh(3.0 * played).astype(np.float32) * 0.3
    return np.concatenate((delay, distorted))[: played.shape[0]]


def ring_cases() -> None:
    ref = EchoRef(SR)
    ref.add(np.ones(SR, dtype=np.float32))
    ref.add(np.full(SR, 2.0, dtype=np.float32))
    tail = ref.recent(1.5)
    check(
        "ring: recent() returns the newest tail, oldest-first",
        tail.shape[0] == int(1.5 * SR)
        and float(tail[-1]) == 2.0
        and float(tail[0]) == 1.0,
        f"len={tail.shape[0]} first={tail[0] if tail.size else '-'} last={tail[-1] if tail.size else '-'}",
    )
    big = EchoRef(SR)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0):  # 7s through a 6s ring
        big.add(np.full(SR, v, dtype=np.float32))
    tail = big.recent(2.0)
    check(
        "ring: wraparound keeps only the newest audio",
        float(tail[0]) == 6.0 and float(tail[-1]) == 7.0,
        f"first={tail[0]} last={tail[-1]}",
    )
    check("ring: empty ring yields empty tail", EchoRef(SR).recent(1.0).size == 0)


def verdict_cases() -> None:
    played = _voice(2.0, seed=1, mod_hz=3.7)

    # Nothing playing: any speech is foreign.
    gate = EchoGate(EchoRef(SR))
    check(
        "gate: speech with a silent reference is foreign",
        gate.foreign(_voice(1.0, seed=3, mod_hz=4.4), SR) is True,
    )

    # Pure echo — delayed + distorted copy of what we played — is NOT foreign.
    ref = EchoRef(SR)
    ref.add(played)
    gate = EchoGate(ref)
    echo = _as_echo(played)[-int(1.2 * SR):]
    is_foreign = gate.foreign(echo, SR)
    check(
        "gate: our own distorted echo is not foreign",
        is_foreign is False,
        f"residual={gate.last_residual}",
    )

    # Echo PLUS an independent second voice IS foreign. Amp 0.25 ≈ talking
    # at comparable loudness to the TTS — the field-tuned 0.50 bar
    # deliberately gives up whispered talk-overs (real own-echo measured
    # 0.30-0.41 on Nate's hardware; a whisper bar can't clear that).
    other = _voice(1.2, seed=9, mod_hz=5.3, phase=1.3, amp=0.25)
    mixed = _as_echo(played)[-int(1.2 * SR):] + other
    is_foreign = gate.foreign(mixed, SR)
    check(
        "gate: a voice talking over the echo is foreign",
        is_foreign is True,
        f"residual={gate.last_residual}",
    )

    # A silent mic never barges, playing or not.
    check(
        "gate: silence is not foreign",
        gate.foreign(np.zeros(SR, dtype=np.float32), SR) is False,
    )

    # COLD START — the live 2026-07-19 false cut: first sentence of a turn,
    # the ring holds only ~0.45s of played audio, and the mic window is the
    # barge onset (300ms pre-roll silence + the first ~0.42s of echo). A
    # short reference must not collapse the alignment search and read the
    # agent's own opening words as foreign.
    cold_ref = EchoRef(SR)
    cold_ref.add(played[: int(0.45 * SR)])
    cold_gate = EchoGate(cold_ref)
    preroll = np.zeros(int(0.3 * SR), dtype=np.float32)
    cold_echo = np.concatenate(
        (preroll, _as_echo(played[: int(0.42 * SR)], delay_s=0.05))
    )
    is_foreign = cold_gate.foreign(cold_echo, SR)
    check(
        "gate: cold-start echo (short ring) is not foreign",
        is_foreign is False,
        f"residual={cold_gate.last_residual}",
    )

    # …and a real voice in that same cold-start window still barges.
    cold_voice = np.concatenate(
        (preroll, _voice(0.42, seed=17, mod_hz=5.1, phase=0.7))
    )
    is_foreign = cold_gate.foreign(cold_voice, SR)
    check(
        "gate: cold-start foreign voice still cuts",
        is_foreign is True,
        f"residual={cold_gate.last_residual}",
    )

    # AGC / COMPRESSION — real mics run automatic gain control: loud
    # syllables get squashed, so dynamic prose echo can't be explained by
    # ONE gain (live 2026-07-19: three self-barges on prose answers while
    # the monotone count passed). The piecewise-gain fit must absorb a
    # slow gain warp…
    agc_ref = EchoRef(SR)
    agc_ref.add(played)
    agc_gate = EchoGate(agc_ref)
    echo = _as_echo(played)[-int(1.2 * SR):]
    env = np.convolve(np.abs(echo), np.ones(1600) / 1600.0, mode="same")
    agc_echo = (echo / np.sqrt(env / (env.mean() + 1e-9) + 0.3)).astype(
        np.float32
    )
    is_foreign = agc_gate.foreign(agc_echo, SR)
    check(
        "gate: AGC-compressed dynamic echo is not foreign",
        is_foreign is False,
        f"residual={agc_gate.last_residual}",
    )

    # …while a real voice over that same AGC-warped echo still cuts (the
    # gain clamp must not 'explain' foreign speech away).
    agc_mixed = agc_echo + _voice(1.2, seed=9, mod_hz=5.3, phase=1.3, amp=0.25)
    is_foreign = agc_gate.foreign(agc_mixed, SR)
    check(
        "gate: voice over AGC-warped echo is foreign",
        is_foreign is True,
        f"residual={agc_gate.last_residual}",
    )

    # SENTENCE BOUNDARY — the live 2026-07-19 self-barge: the mic window
    # spans [end of sentence 1][render pause][start of sentence 2]. The ring
    # must record that pause as timeline zeros; a gapless sample-appender
    # can't explain the window at any single alignment and the agent's own
    # voice reads as foreign ('▸ you (barge-in) Up to you, what are…').
    import time as _time

    s1 = _voice(1.0, seed=4, mod_hz=3.9)
    s2 = _voice(1.0, seed=21, mod_hz=4.6, phase=0.4)
    tl_ref = EchoRef(SR)
    tl_gate = EchoGate(tl_ref)
    tl_ref.add(s1)
    _time.sleep(1.35)  # 1.0s "playback" of s1 + a 0.35s render pause
    tl_ref.add(s2)
    boundary_src = np.concatenate(
        (
            s1[-int(0.3 * SR):],
            np.zeros(int(0.35 * SR), dtype=np.float32),
            s2[: int(0.4 * SR)],
        )
    )
    is_foreign = tl_gate.foreign(_as_echo(boundary_src, delay_s=0.05), SR)
    check(
        "gate: echo across a sentence pause is not foreign",
        is_foreign is False,
        f"residual={tl_gate.last_residual}",
    )

    # STALE RING — playback ended a while ago; a voice now must be foreign
    # even though old audio still sits in the ring (the tail-gap zeros are
    # what keep yesterday's sentence from 'explaining' today's speech).
    _time.sleep(1.6)
    is_foreign = tl_gate.foreign(_voice(1.0, seed=33, mod_hz=5.6), SR)
    check(
        "gate: speech after playback ended is foreign (stale ring)",
        is_foreign is True,
        f"residual={tl_gate.last_residual}",
    )


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


class _StubGate:
    """Scripted-verdict gate for wiring tests. The first calls come from the
    main listen loop (between turns nothing is playing, so the real gate says
    foreign there) — script those, then fall back to `default` for the
    barge-onset checks during the turn."""

    def __init__(self, verdicts: list[bool], default: bool) -> None:
        self.verdicts = list(verdicts)
        self.default = default
        self.calls = 0

    def foreign(self, audio, sample_rate) -> bool:
        self.calls += 1
        return self.verdicts.pop(0) if self.verdicts else self.default


class _BargeBackend(FakeBackend):
    """Turn 1 completes only when interrupted; turn 2 replies normally."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            await self._q.put(AssistantTextDelta(text="Let me explain at length. "))
            await self._q.put(AssistantTextDelta(text="There are many details. "))
        else:
            await self._q.put(AssistantTextDelta(text="Okay, stopping."))
            await self._q.put(TurnComplete(text="Okay, stopping."))

    async def interrupt(self) -> None:
        await super().interrupt()
        await self._q.put(TurnComplete(text="Let me explain at length."))


class _PlainBackend(FakeBackend):
    """Every turn completes normally (no interrupt needed)."""

    async def send(self, user_text: str) -> None:
        self.sent.append(user_text)
        await self._q.put(AssistantTextDelta(text="Answering now. "))
        await self._q.put(TurnComplete(text="Answering now."))


class _CountingSpeaker(FakeSpeaker):
    def __init__(self, sample_rate: int = 16000) -> None:
        super().__init__(sample_rate)
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


async def echo_never_cuts_case() -> None:
    """Speech-shaped frames during the agent's turn that the gate calls
    'my own echo' must not cut the turn OR become a user message."""
    cfg = Config(
        silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False
    )
    frames = _speech(15) + _silence(60) + _speech(25) + _silence(40)
    backend = _PlainBackend()
    speaker = _CountingSpeaker(SR)
    gate = _StubGate([True], default=False)
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Question one.", "SHOULD NEVER SEND"]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=speaker,
        echo_gate=gate,
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    check(
        "echo-no-cut: only the real question reached the agent",
        backend.sent == ["Question one."],
        str(backend.sent),
    )
    check("echo-no-cut: never interrupted", backend.interrupts == 0)
    check("echo-no-cut: the gate was consulted for the barge", gate.calls >= 2)


def own_speech_matcher_cases() -> None:
    """The echo-transcript backstop: barge transcripts that are fragments of
    what the agent just said (incl. STT mishears) must match; real user
    speech must not. Exercises the live 2026-07-19 false messages."""
    from talk2me.render import PlainRenderer

    orch = object.__new__(Orchestrator)
    orch._echo_gate = object()  # any non-None: AEC mode
    orch._spoken_texts = [
        "Quantum physics is the branch of physics that describes how nature "
        "behaves at the smallest scales.",
        "Keep an eye out for afternoon thunderstorms, which are common this "
        "time of year.",
        "Yeah, thunderstorms are the most common severe weather in NYC "
        "during summer.",
    ]
    orch.render = PlainRenderer()

    for text in (
        "Physics is the branch of.",
        "which are commonest.",
        "thunderstorms are them.",
    ):
        check(
            f"backstop: live false message matches: {text!r}",
            orch._own_speech_echo(text) is True,
        )
    for text in (
        "Okay, great job. Now talk to me about quantum physics.",
        "I was asking about quantum physics.",
        "Tell me about the weather tomorrow instead.",
        "stop",  # short fragments never swallowed
    ):
        check(
            f"backstop: real user speech passes: {text!r}",
            orch._own_speech_echo(text) is False,
        )
    orch._spoken_texts = []
    check(
        "backstop: silent turn never matches",
        orch._own_speech_echo("anything at all here") is False,
    )


async def echo_transcript_swallowed_case() -> None:
    """A false cut whose transcript is the agent's own sentence must not
    become a user message — the session returns to listening."""
    cfg = Config(
        silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False
    )
    frames = _speech(15) + _silence(80) + _speech(45) + _silence(35)
    backend = _BargeBackend()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        # The "barge" transcribes to a fragment of the agent's own reply.
        stt=FakeSTT(["Question one.", "Let me explain at length."]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=_CountingSpeaker(SR),
        echo_gate=_StubGate([True], default=True),
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    check(
        "swallow: the echo transcript never reached the agent",
        backend.sent == ["Question one."],
        str(backend.sent),
    )
    check("swallow: the turn was still cut once", backend.interrupts == 1)


async def flicker_never_cuts_case() -> None:
    """One foreign verdict followed by an echo verdict must NOT cut — the
    double-confirm exists because live echo scores flicker near the bar."""
    cfg = Config(
        silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False
    )
    frames = _speech(15) + _silence(60) + _speech(45) + _silence(40)

    class _SlowBackend(FakeBackend):
        """Turn 1 stays open ~1.5s (so the monitor owns the mic while the
        flickering 'foreign' speech arrives), then completes on its own."""

        async def send(self, user_text: str) -> None:
            self.sent.append(user_text)
            await self._q.put(AssistantTextDelta(text="Thinking it through. "))
            asyncio.get_running_loop().call_later(
                1.5,
                lambda: self._q.put_nowait(
                    TurnComplete(text="Thinking it through.")
                ),
            )

    backend = _SlowBackend()
    gate = _StubGate([True, True, False], default=False)
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Question one.", "SHOULD NEVER SEND"]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=_CountingSpeaker(SR),
        echo_gate=gate,
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    check(
        "flicker: a one-off foreign verdict never cut",
        backend.sent == ["Question one."] and backend.interrupts == 0,
        f"sent={backend.sent} interrupts={backend.interrupts}",
    )
    check("flicker: the confirm window was actually used", gate.calls >= 3)


async def foreign_cuts_case() -> None:
    """The same frames judged 'foreign' must cut the turn like classic barge."""
    cfg = Config(
        silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False
    )
    # 45 speech frames: the echo-gated cut needs onset (650ms) + the
    # 300ms confirm window before the point of no return.
    frames = _speech(15) + _silence(80) + _speech(45) + _silence(35)
    backend = _BargeBackend()
    speaker = _CountingSpeaker(SR)
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Question one.", "Actually stop."]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=speaker,
        echo_gate=_StubGate([True], default=True),
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    check(
        "foreign-cut: the talk-over became the next turn",
        backend.sent == ["Question one.", "Actually stop."],
        str(backend.sent),
    )
    check("foreign-cut: exactly one interrupt", backend.interrupts == 1)
    check("foreign-cut: playback was stopped", speaker.stops >= 1)


def main() -> int:
    ring_cases()
    verdict_cases()
    own_speech_matcher_cases()
    asyncio.run(echo_never_cuts_case())
    asyncio.run(flicker_never_cuts_case())
    asyncio.run(foreign_cuts_case())
    asyncio.run(echo_transcript_swallowed_case())
    failed = [n for n, ok in RESULTS if not ok]
    print(
        f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

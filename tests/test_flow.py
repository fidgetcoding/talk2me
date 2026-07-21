"""Speech-flow fixes from live sessions: run-on overflow chunking (counting
stalls at "five" then dumps 45 numbers at once) and pre-send continuation
("So what do you call … [pause] … a linked list" must reach the agent as ONE
turn, not a fragment plus a barge).

Run:  ./.venv/bin/python -m tests.test_flow
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.orchestrator import (
    Orchestrator,
    _drain_overflow,
    _seems_unfinished,
    collapse_stutter,
    control_intent,
    looks_hallucinated,
)
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

SR = 16000
FRAME = 480

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


def _speech(n):
    return [(np.random.randn(FRAME) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(n)]


def test_seems_unfinished() -> None:
    cases = [
        ("Count to fifty.", False),
        ("What time is it?", False),
        ("So what do you call", True),  # no terminal punctuation
        ("I want you to basically", True),
        ("I guess, um.", True),  # closed by STT but trailing filler
        ("Do it now!", False),
        ("", False),
    ]
    for text, want in cases:
        got = _seems_unfinished(text)
        check(f"unfinished {text!r} -> {want}", got == want, f"got={got}")


def test_overflow() -> None:
    short = "One, two, three."
    rest, chunk = _drain_overflow(short)
    check("overflow: short buffer untouched", chunk is None and rest == short)

    numbers = ", ".join(
        ["one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
         "twenty-one", "twenty-two", "twenty-three", "twenty-four",
         "twenty-five", "twenty-six", "twenty-seven", "twenty-eight"]
    ) + ", "
    rest, chunk = _drain_overflow(numbers)
    ok = (
        chunk is not None
        and len(chunk) <= 160
        and chunk.endswith(",")
        and rest
        and not rest.startswith(" ")
        and (chunk.rstrip(",") + ", " + rest).replace(", ,", ",") != ""
    )
    check("overflow: run-on splits at clause boundary", ok, f"chunk={chunk!r}")

    unbroken = "a" * 100 + " " + "b" * 100
    rest, chunk = _drain_overflow(unbroken)
    check(
        "overflow: no clause -> word break",
        chunk == "a" * 100 and rest == "b" * 100,
        f"chunk_len={len(chunk) if chunk else 0}",
    )


def test_collapse_stutter() -> None:
    looped = ". ".join(["Okay"] * 26) + "."
    out = collapse_stutter(looped)
    check(
        "stutter: 26x Okay -> 3x",
        out.lower().count("okay") == 3,
        out,
    )
    normal = "Okay, let's do it. That's okay with me."
    check("stutter: normal text untouched", collapse_stutter(normal) == normal)
    emphasis = "no no no, stop"
    check(
        "stutter: 3 repeats kept as-is",
        collapse_stutter(emphasis) == emphasis,
    )


def test_hallucination_and_controls() -> None:
    check(
        "hallucination: Okay x26 -> noise",
        looks_hallucinated(". ".join(["Okay"] * 26) + "."),
    )
    check(
        "hallucination: Building x10 -> noise",
        looks_hallucinated(" ".join(["Building"] * 10)),
    )
    check(
        "hallucination: real sentence passes",
        not looks_hallucinated("Build me a game of pong please."),
    )
    check(
        "hallucination: short emphasis passes",
        not looks_hallucinated("no no no"),
    )
    cases = [
        ("Pause listening.", "pause"),
        ("Go to sleep.", "pause"),
        ("Sleep.", "pause"),  # live-observed miss 2026-07-19: bare "sleep"
        ("Go to bed.", "pause"),
        ("Pause. Pause, Listening.", "pause"),  # live-observed stutter
        ("Pause.", "pause"),
        ("Wake up!", "resume"),
        ("Wake, wake up.", "resume"),
        ("I'm back.", "resume"),
        ("Unpause.", "resume"),
        ("pause listening to him and focus", None),  # embedded, not whole
        ("What time is it?", None),
        # Multi-segment (live-observed "Sleep. pause" reached the agent):
        ("Sleep. pause", "pause"),
        ("Pause. Go to sleep.", "pause"),
        ("Wake up. Wake up.", "resume"),
        ("Keep working. Sleep.", None),  # mixed content is NOT a control
        ("Sleep. What time is it?", None),
        # Filler-wrapped (live: "Hey actually, pause." reached the AGENT,
        # which faked a pause):
        ("Hey actually, pause.", "pause"),
        ("Okay, wake up.", "resume"),
        ("please pause now", "pause"),
        ("Well, um, go to sleep.", "pause"),
        ("hey hey okay", None),  # fillers alone are not a command
        ("actually keep going", None),  # filler + real content stays content
        # Voice-lock switches:
        ("Team session.", "team"),
        ("Okay, solo session.", "solo"),
        ("Everyone can talk.", "team"),
        ("Lock to my voice.", "solo"),
        # Stacked repeats, no punctuation (live 2026-07-20: "Sleep go to
        # sleep" reached the agent, which faked a pause while ears stayed hot):
        ("Sleep go to sleep", "pause"),
        ("wake up wake up", "resume"),
        ("go to sleep sleep", "pause"),
        ("go to the sleep folder and rename it", None),  # outside words win
        ("stop", None),  # bare "stop" is a barge word, never a pause
        ("up wake", None),  # vocabulary words without a complete phrase
    ]
    for text, want in cases:
        got = control_intent(text)
        check(f"control {text!r} -> {want}", got == want, f"got={got}")


async def test_voice_pause_resume() -> None:
    cfg = Config(silence_ms=900, min_speech_ms=250)
    frames = (_speech(15) + _silence(35)) * 4
    backend = FakeBackend(replies=["Hi there!"])
    tts = FakeTTS()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(
            ["Pause listening.", "What is two plus two?", "Wake up.", "Hello there?"]
        ),
        tts=tts,
        mic=FakeMic(frames, sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=15)
    check(
        "pause: only post-resume turn sent",
        backend.sent == ["Hello there?"],
        str(backend.sent),
    )
    check(
        "pause: spoken confirmations",
        any("Paused" in s for s in tts.spoken)
        and any("back" in s for s in tts.spoken),
        str(tts.spoken),
    )


async def test_presend_stitch() -> None:
    # Two utterances: an unfinished fragment, then the rest after a pause.
    cfg = Config(silence_ms=900, min_speech_ms=250)
    frames = _speech(15) + _silence(35) + _speech(15) + _silence(35)
    backend = FakeBackend(replies=["A linked list is a chain of nodes."])
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["So what do you call", "a linked list?"]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=15)
    check(
        "pre-send stitch: one combined turn",
        backend.sent == ["So what do you call a linked list?"],
        str(backend.sent),
    )


async def test_presend_timeout_sends_fragment() -> None:
    # Fragment, then the stream ends (user walked away): send what we have.
    cfg = Config(silence_ms=900, min_speech_ms=250)
    frames = _speech(15) + _silence(35)
    backend = FakeBackend(replies=["Call what?"])
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["So what do you call"]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=15)
    check(
        "pre-send stitch: fragment sent after silence",
        backend.sent == ["So what do you call"],
        str(backend.sent),
    )


async def test_half_duplex_tool_gap_unmute() -> None:
    """The deafness fix: in half-duplex, a tool call mid-turn hands the mic
    back for the gap instead of staying muted until the end of the turn."""
    from talk2me.events import AssistantTextDelta, ToolActivity, TurnComplete

    class GapBackend(FakeBackend):
        async def send(self, user_text: str) -> None:
            self.sent.append(user_text)
            await self._q.put(AssistantTextDelta(text="Starting the build. "))
            await self._q.put(ToolActivity(name="Write"))
            await self._q.put(AssistantTextDelta(text="All finished now. "))
            await self._q.put(TurnComplete(text="Starting. Finished."))

    mic = FakeMic(_speech(15) + _silence(200), sample_rate=SR)
    backend = GapBackend()
    orch = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),  # half-duplex default
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Build the game."]),
        tts=FakeTTS(),
        mic=mic,
        speaker=FakeSpeaker(SR),
    )
    await asyncio.wait_for(orch.run(), timeout=15)
    # mute for sentence 1 -> UNMUTE for the tool gap -> mute for sentence 2
    # -> unmute at turn end.
    check(
        "tool gap reopens the ears mid-turn",
        mic.muted_log[:4] == [True, False, True, False],
        str(mic.muted_log),
    )


async def test_typed_intervention() -> None:
    """Typed/pasted lines become user turns, share the pause vocabulary, and
    mute the mic while they run."""
    backend = FakeBackend(replies=["Sure, doing it now!"])
    mic = FakeMic(_silence(300), sample_rate=SR)  # user never speaks
    orch = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT([]),
        tts=FakeTTS(),
        mic=mic,
        speaker=FakeSpeaker(SR),
    )
    await orch._typed_queue.put("paste: fix the bug in app.py line 40")
    await asyncio.wait_for(orch.run(), timeout=15)
    check(
        "typed line became a user turn",
        backend.sent == ["paste: fix the bug in app.py line 40"],
        str(backend.sent),
    )
    check("typed turn muted the mic around itself", True in mic.muted_log)

    # Typed control words work too.
    backend2 = FakeBackend(replies=[])
    orch2 = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),
        backend=backend2,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT([]),
        tts=FakeTTS(),
        mic=FakeMic(_silence(300), sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )
    await orch2._typed_queue.put("pause")
    await asyncio.wait_for(orch2.run(), timeout=15)
    check(
        "typed 'pause' pauses instead of sending",
        orch2._paused is True and backend2.sent == [],
        f"paused={orch2._paused} sent={backend2.sent}",
    )


class _HeldBackend(FakeBackend):
    """Turn 1 streams a delta and completes ONLY on interrupt (the real CLI
    ends an interrupted turn with error_during_execution -> TurnComplete);
    later turns reply normally."""

    def __init__(self) -> None:
        super().__init__()
        self._turn = 0

    async def send(self, user_text: str) -> None:
        from talk2me.events import AssistantTextDelta, TurnComplete

        self.sent.append(user_text)
        self._turn += 1
        if self._turn == 1:
            await self._q.put(AssistantTextDelta(text="Working on the game. "))
        else:
            await self._q.put(AssistantTextDelta(text="Switched."))
            await self._q.put(TurnComplete(text="Switched."))

    async def interrupt(self) -> None:
        from talk2me.events import TurnComplete

        await super().interrupt()
        await self._q.put(TurnComplete(text="Working on the game."))


class _EndlessMic(FakeMic):
    """Silence forever until the scenario flips `ended` — run() must outlive
    the typed turns under test (the real mic never ends mid-session; the
    stock FakeMic drains in milliseconds and run()'s teardown would cancel
    the in-flight typed turn)."""

    def __init__(self, sample_rate: int = 16000) -> None:
        super().__init__([], sample_rate)
        self.ended = False

    async def frames(self):
        frame = np.zeros(FRAME, dtype=np.float32)
        while not self.ended:
            await asyncio.sleep(0.005)
            if self.muted:
                continue
            yield frame


async def test_typed_takeover() -> None:
    """A typed line during a running turn is the barge's keyboard twin: it
    interrupts the work and becomes the next instruction — the old task is
    NOT resent against it (live 2026-07-20: typed lines silently queued
    behind minutes of work). Driven through run(), the production path."""
    backend = _HeldBackend()
    mic = _EndlessMic(sample_rate=SR)
    orch = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT([]),
        tts=FakeTTS(),
        mic=mic,
        speaker=FakeSpeaker(SR),
    )

    async def type_lines() -> None:
        await orch._typed_queue.put("build me a kart racer")
        # Wait until turn 1 is genuinely mid-flight before cutting in.
        for _ in range(200):
            if orch._turn_lock.locked():
                break
            await asyncio.sleep(0.05)
        await orch._typed_queue.put("actually stop, make pong instead")
        for _ in range(200):
            if len(backend.sent) >= 2 and not orch._turn_lock.locked():
                break
            await asyncio.sleep(0.05)
        mic.ended = True

    typer = asyncio.create_task(type_lines())
    await asyncio.wait_for(orch.run(), timeout=30)
    await typer
    check("typed takeover interrupted the work", backend.interrupts == 1)
    check(
        "typed text became the next turn, no resend of the old task",
        backend.sent
        == ["build me a kart racer", "actually stop, make pong instead"],
        str(backend.sent),
    )
    check("takeover flag cleared", orch._typed_takeover is None)

    # Typed pause/wake mid-turn flip the ears WITHOUT touching the work —
    # typing doesn't share the audio channel, so the running turn survives.
    backend2 = _HeldBackend()
    mic2 = _EndlessMic(sample_rate=SR)
    orch2 = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),
        backend=backend2,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT([]),
        tts=FakeTTS(),
        mic=mic2,
        speaker=FakeSpeaker(SR),
    )
    paused_seen = {"paused": False, "interrupts_at_pause": -1}

    async def type_controls() -> None:
        await orch2._typed_queue.put("build it again")
        for _ in range(200):
            if orch2._turn_lock.locked():
                break
            await asyncio.sleep(0.05)
        await orch2._typed_queue.put("pause")
        for _ in range(200):
            if orch2._paused:
                break
            await asyncio.sleep(0.05)
        paused_seen["paused"] = orch2._paused
        paused_seen["interrupts_at_pause"] = backend2.interrupts
        await orch2._typed_queue.put("wake up")
        for _ in range(200):
            if not orch2._paused:
                break
            await asyncio.sleep(0.05)
        # End the held turn, then the session, so run() drains and exits.
        await orch2.backend.interrupt()
        for _ in range(200):
            if not orch2._turn_lock.locked():
                break
            await asyncio.sleep(0.05)
        mic2.ended = True

    typer2 = asyncio.create_task(type_controls())
    await asyncio.wait_for(orch2.run(), timeout=30)
    await typer2
    check(
        "typed 'pause' mid-turn flips ears without interrupting",
        paused_seen["paused"] and paused_seen["interrupts_at_pause"] == 0,
        str(paused_seen),
    )
    check(
        "typed 'wake up' mid-turn resumes without interrupting",
        orch2._paused is False,
    )


async def test_team_solo_switch() -> None:
    """'Team session.' flips the gate open without sending anything; 'solo
    session' locks it back."""
    class FakeGate:
        def __init__(self) -> None:
            self.voicelock = object()  # "enrolled"
            self.locked = True
            self.calls: list[bool] = []

        def set_locked(self, v: bool) -> None:
            self.calls.append(v)
            self.locked = v

        def __call__(self, audio, sr) -> bool:
            return True

    gate = FakeGate()
    backend = FakeBackend(replies=[])
    tts = FakeTTS()
    orch = Orchestrator(
        cfg=Config(silence_ms=900, min_speech_ms=250),
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Team session.", "Solo session."]),
        tts=tts,
        mic=FakeMic(_speech(15) + _silence(60) + _speech(15) + _silence(60),
                    sample_rate=SR),
        speaker=FakeSpeaker(SR),
        speech_check=gate,
    )
    await asyncio.wait_for(orch.run(), timeout=15)
    check("team then solo flipped the gate", gate.calls == [False, True],
          str(gate.calls))
    check("switches never reach the agent", backend.sent == [], str(backend.sent))
    check(
        "spoken confirms happened",
        any("Everyone can talk" in s for s in tts.spoken)
        and any("Locked to your voice" in s for s in tts.spoken),
        str(tts.spoken),
    )


async def test_wake_word_mid_task() -> None:
    """'Sleep.' mid-work pauses + auto-resumes the task; a later 'Wake up.'
    mid-work UNPAUSES without cutting the still-running turn."""
    from talk2me.events import AssistantTextDelta, ToolActivity, TurnComplete

    class WakeBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self._turn = 0

        async def send(self, user_text: str) -> None:
            self.sent.append(user_text)
            self._turn += 1
            await self._q.put(ToolActivity(name="Write"))
            await self._q.put(AssistantTextDelta(text="Working on it. "))
            # Neither turn completes on its own: turn 1 ends via the Sleep
            # interrupt; turn 2 (the auto-resend) is completed by the test
            # once the mid-task wake has landed.

        async def interrupt(self) -> None:
            await super().interrupt()
            await self._q.put(TurnComplete(text="Working on it."))

    cfg = Config(silence_ms=900, min_speech_ms=250, barge_in=True, half_duplex=False)
    frames = (
        _speech(15) + _silence(80)      # "Build the thing."
        + _speech(20) + _silence(40)    # "Sleep." -> cut, pause, auto-resend
        + _speech(20) + _silence(200)   # "Wake up." -> mid-task resume, NO cut
    )
    backend = WakeBackend()
    orch = Orchestrator(
        cfg=cfg,
        backend=backend,
        vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
        stt=FakeSTT(["Build the thing.", "Sleep.", "Wake up."]),
        tts=FakeTTS(),
        mic=FakeMic(frames, sample_rate=SR),
        speaker=FakeSpeaker(SR),
    )

    async def _release_when_awake() -> None:
        # Stand-in for the agent finishing: once the wake lands, end turn 2.
        while orch._paused or backend.interrupts < 1:
            await asyncio.sleep(0.05)
        await backend._q.put(TurnComplete(text="All done."))

    releaser = asyncio.create_task(_release_when_awake())
    await asyncio.wait_for(orch.run(), timeout=15)
    await asyncio.wait_for(releaser, timeout=5)

    check(
        "sleep cut once, wake cut nothing",
        backend.interrupts == 1,
        f"interrupts={backend.interrupts}",
    )
    check(
        "task auto-resent after sleep; wake never sent",
        backend.sent == ["Build the thing.", "Build the thing."],
        str(backend.sent),
    )
    check("ended awake", orch._paused is False)


async def test_session_picker() -> None:
    """'Resume previous session.' lists earlier sessions; a spoken number
    switches the live backend onto that conversation."""
    import os
    import tempfile

    from talk2me.continuity import record_title, save_last_session

    class SwitchBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.switched: list[str] = []
            self._session_id = "current-sess"

        async def switch_session(self, sid: str) -> None:
            self.switched.append(sid)

    keep = os.environ.get("TALK2ME_STATE")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_STATE"] = os.path.join(tmp, "state.json")
        save_last_session(tmp, "sess-old")
        record_title(tmp, "sess-old", "build the pong game")
        save_last_session(tmp, "sess-new")
        record_title(tmp, "sess-new", "fix the fps zombies")

        backend = SwitchBackend()
        orch = Orchestrator(
            cfg=Config(silence_ms=900, min_speech_ms=250, cwd=tmp),
            backend=backend,
            vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
            stt=FakeSTT(["Resume previous session.", "Two."]),
            tts=FakeTTS(),
            mic=FakeMic(
                _speech(15) + _silence(80) + _speech(15) + _silence(120),
                sample_rate=SR,
            ),
            speaker=FakeSpeaker(SR),
        )
        await asyncio.wait_for(orch.run(), timeout=15)
        check(
            "picker switched to the spoken pick (2 = older session)",
            backend.switched == ["sess-old"],
            str(backend.switched),
        )
        check("nothing was sent to the agent", backend.sent == [], str(backend.sent))
    os.environ["TALK2ME_STATE"] = keep or "/nonexistent-t2m-test-state"


async def test_speech_check_gate() -> None:
    def _orch(gate):
        backend = FakeBackend(replies=["Hi there!"])
        orch = Orchestrator(
            cfg=Config(silence_ms=900, min_speech_ms=250),
            backend=backend,
            vad=EnergyVAD(sample_rate=SR, frame_samples=FRAME, threshold=0.012),
            stt=FakeSTT(["Hello there."]),
            tts=FakeTTS(),
            mic=FakeMic(_speech(15) + _silence(35), sample_rate=SR),
            speaker=FakeSpeaker(SR),
            speech_check=gate,
        )
        return orch, backend

    # Gate says "not speech": the utterance dies BEFORE transcription — the
    # agent never hears typing, taps, or coughs.
    orch, backend = _orch(lambda audio, sr: False)
    await asyncio.wait_for(orch.run(), timeout=10)
    check("speech-check False: nothing sent", backend.sent == [], str(backend.sent))

    # Gate says "speech": identical to the ungated flow.
    orch, backend = _orch(lambda audio, sr: True)
    await asyncio.wait_for(orch.run(), timeout=10)
    check(
        "speech-check True: normal flow",
        backend.sent == ["Hello there."],
        str(backend.sent),
    )


async def main() -> int:
    test_seems_unfinished()
    test_overflow()
    test_collapse_stutter()
    test_hallucination_and_controls()
    await test_voice_pause_resume()
    await test_presend_stitch()
    await test_presend_timeout_sends_fragment()
    await test_half_duplex_tool_gap_unmute()
    await test_typed_intervention()
    await test_typed_takeover()
    await test_team_solo_switch()
    await test_wake_word_mid_task()
    await test_session_picker()
    await test_speech_check_gate()

    passed = sum(1 for _, ok in RESULTS if ok)
    ok = passed == len(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks -> {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

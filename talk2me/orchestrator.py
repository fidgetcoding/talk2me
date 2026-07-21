"""The turn-taking voice loop. Owns the conversation; depends only on Protocols.

Half-duplex flow (the default — no echo-cancellation hardware needed):

    listen -> segment one utterance -> transcribe -> send to agent
           -> stream agent text, speaking sentence-by-sentence (mic muted)
           -> hand the mic back, listen again

Full-duplex barge-in (--barge-in, requires headphones so the mic never hears
the TTS): the mic stays live while the agent speaks. A monitor task watches the
frames; on sustained speech it stops the Speaker, sends the backend a real
interrupt (the CLI cancels generation and ends the turn — spike-verified), and
keeps collecting the interrupting utterance, which becomes the next user turn.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import AsyncIterator

import numpy as np

from .audio import Mic, Speaker
from .config import Config
from .events import (
    AgentEvent,
    AssistantTextDelta,
    BackendError,
    PermissionRequest,
    SessionReady,
    ThinkingDelta,
    ToolActivity,
    TurnComplete,
)
from .continuity import list_sessions, record_title, save_last_session
from .protocols import STT, TTS, VAD, AgentBackend
from .render import PlainRenderer, Renderer
from .segment import segment_utterances

_SENTENCE = re.compile(r"(.+?[.!?]+)(\s+|$)", re.DOTALL)

# How long to wait for the user to START speaking again after an unfinished-
# sounding transcript, per extension (at most 3 extensions). Once speech has
# started, the utterance is always heard out — the timeout never cuts a
# sentence mid-air (the old total-time window did, live).
CONTINUATION_WAIT_S = 6.0
MAX_CONTINUATION_EXTENSIONS = 3

# Trailing words that mean the speaker hasn't finished the thought, even when
# the STT appended a period.
_TRAILING_FILLERS = frozenset(
    "um uh like so and but or basically the a an to of for with i you know "
    "guess mean".split()
)


def _seems_unfinished(text: str) -> bool:
    """True when a transcript reads as a cut-off thought.

    Both whisper and parakeet append terminal punctuation when an utterance
    SOUNDS complete ("Count to fifty.") and omit it when the speaker trails
    off ("So what do you call") — that's the primary signal, with a trailing-
    filler check for sentences the STT closed anyway ("I guess, um.").
    """
    t = text.rstrip()
    if not t:
        return False
    if t[-1] not in ".!?":
        return True
    words = re.findall(r"[a-z']+", t.lower())
    return bool(words) and words[-1] in _TRAILING_FILLERS


# Working-tick cadence: while the agent is mid-tool-run and nothing has been
# audible for this long, play a soft system blip so silence reads as "working",
# not "dead". The blip is ~150ms — far under the 450ms barge onset, so it can
# never cut a turn even in full duplex.
WORKING_TICK_QUIET_S = 8.0
_TICK_SOUND = "/System/Library/Sounds/Tink.aiff"

# Sustained speech needed before the monitor CUTS a turn (barge/continuation).
# Deliberately higher than min_speech_ms: a cut kills the agent's turn, so a
# breath, chair squeak, or trailing word must not fire it — live sessions
# showed 250ms onsets false-triggering and eating turns. A real "okay stop"
# easily sustains 450ms.
BARGE_ONSET_MIN_MS = 450

# Hard ceiling on a barge-in utterance. An interruption is a sentence, not a
# speech: without this, a user venting for 20s+ is silently "collected" and
# everything processes way late — which reads as the whole app hanging.
BARGE_MAX_UTTERANCE_MS = 10_000

# How long to wait for a spoken approve/deny before treating the attempt as
# unclear. The CLI side blocks indefinitely (verified), so this is purely a
# never-leave-an-approval-hanging safety: two silent attempts end in a deny.
PERMISSION_LISTEN_TIMEOUT_S = 60.0

# Spoken-intent grammar for the permission gate. Deny is checked FIRST and wins
# on any overlap ("no, don't do it" contains both kinds of token) — never
# auto-allow on ambiguity.
_DENY_TOKENS = frozenset(
    "deny denied no nope stop cancel reject rejected skip never negative "
    "don't dont".split()
)
_APPROVE_TOKENS = frozenset(
    "approve approved yes yeah yep yup go allow allowed sure okay ok confirm "
    "affirmative".split()
)
_APPROVE_PHRASES = ("do it", "go ahead")

# Tokens that LOOK like sentence ends but aren't — don't split right after these.
# Compared lowercased, with the trailing period stripped by the chunker.
_ABBREVIATIONS = frozenset(
    {
        "e.g",
        "i.e",
        "dr",
        "mr",
        "mrs",
        "ms",
        "vs",
        "etc",
        "st",
        "jr",
        "sr",
        "prof",
        "no",
    }
)


class Orchestrator:
    def __init__(
        self,
        *,
        cfg: Config,
        backend: AgentBackend,
        vad: VAD,
        stt: STT,
        tts: TTS,
        mic: Mic,
        speaker: Speaker,
        session_log=None,
        renderer: Renderer | None = None,
        speech_check=None,
        echo_gate=None,
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.mic = mic
        self.speaker = speaker
        self.log = session_log  # SessionLog | None — duck-typed, optional
        # Every user-facing line goes through this seam. The default is the
        # launch build's exact output (parity-locked by tests/test_render.py),
        # which is also why existing suites construct without it and pass.
        self.render: Renderer = renderer or PlainRenderer()
        # Optional speech confirmation gate: (audio, sample_rate) -> bool.
        # Injected by production wiring (Silero via speechcheck.py); None in
        # tests, whose synthetic "speech" is random noise a real classifier
        # would reject. Guards the barge cut and the pre-transcribe path.
        self._speech_check = speech_check
        # Optional echo gate (speakers full duplex): foreign(audio, rate) is
        # False when the sound is just our own TTS coming back off the
        # speakers. Guards the barge cut AND the main listen loop, so the
        # agent can neither be interrupted by nor talk to its own echo.
        self._echo_gate = echo_gate
        # True right after "🎧 listening…" printed with nothing after it —
        # suppresses the repeat-spam that noise utterances used to cause.
        self._listening_fresh = False
        # Typed intervention: lines typed/pasted into the terminal become
        # user turns. The lock serializes typed turns with voice turns so
        # only one owns the backend (and the mic) at a time.
        self._typed_queue: asyncio.Queue[str] = asyncio.Queue()
        self._stdin_lines: asyncio.Queue[str] = asyncio.Queue()
        self._turn_lock = asyncio.Lock()
        self._events = None  # the backend event iterator, shared across paths
        self._titled = False  # first user message becomes the session title
        self._lock_rejects = 0  # consecutive voice-lock rejections (hint gate)
        # The last instruction that reached the agent — continuation stitching
        # and the noise/pause recovery resends read it; typed turns update it
        # too so an interrupted typed task can auto-resume.
        self._last_text = ""
        # Set when a turn dies on a BackendError; tells run() to stop cleanly
        # instead of looping forever against a dead backend process.
        self._fatal = False
        # Voice-commanded pause: while True, transcripts are heard but never
        # sent — only a resume command gets through.
        self._paused = False
        # Barge-in state, reset per turn: the monitor task watching the live
        # mic, and whether it already cut this turn's playback.
        self._barge_task: asyncio.Task[str | None] | None = None
        self._interrupted = False
        self._spoke_any = False
        # Set by _consume_turn: the monitor cut the turn BEFORE any agent
        # speech, so the captured utterance continues the user's previous turn.
        self._continuation = False
        # When the current user turn was handed to the backend; drives the
        # --debug latency lines (first-token / first-audio).
        self._t_sent = 0.0
        # Sentences enqueued for speech THIS turn — the echo-transcript
        # backstop compares a barge transcript against them: a "user
        # message" that is a fragment of what the agent itself just said is
        # its own echo that slipped the gate, and must never reach the
        # backend (live 2026-07-19: it answered its own words three times).
        self._spoken_texts: list[str] = []
        # Per-turn render/play pipeline: sentence n+1 renders WHILE sentence n
        # plays, so the voice never stalls between chunks waiting on `say`.
        self._render_q: asyncio.Queue[str | None] | None = None
        self._blocks_q: asyncio.Queue[list | None] | None = None
        self._render_task: asyncio.Task[None] | None = None
        self._play_task: asyncio.Task[None] | None = None
        # Working-tick state: last time anything was audible, whether the
        # current turn has run a tool (and how many), and whether afplay
        # proved unavailable.
        self._last_audible = 0.0
        self._saw_tool = False
        self._tool_count = 0
        self._ticks_broken = False

    async def run(self) -> None:
        await self.backend.start()
        self.mic.start()
        events = self.backend.events()
        # Pre-load the STT model BEFORE opening the ears. Loading concurrently
        # with live listening looked clever but a session froze inside that
        # window on real hardware (model load + HF hub check while the first
        # utterance streamed); a second of visible startup is the honest cost.
        # Duck-typed: engines without warmup() lazy-load on first use.
        if hasattr(self.stt, "warmup"):
            self.render.loading_ears()
            await asyncio.to_thread(self.stt.warmup)
        if self._speech_check is not None and hasattr(self._speech_check, "warmup"):
            # Load the speech classifier BEFORE the mic opens — its first call
            # must never add latency inside the barge monitor's cut decision.
            await asyncio.to_thread(self._speech_check.warmup)
        self.render.startup(self.cfg)
        self._events = events
        # Typed intervention: a worker turns typed/pasted lines into user
        # turns (serialized with voice turns via _turn_lock). The stdin pump
        # thread only exists on a real terminal; tests feed _typed_queue.
        typed_task = asyncio.create_task(self._typed_worker())
        assembler_task: asyncio.Task | None = None
        if sys.stdin is not None and sys.stdin.isatty():
            self._start_stdin_thread()
            assembler_task = asyncio.create_task(self._stdin_assembler())
        try:
            self._show_listening()
            async for utterance in segment_utterances(
                self.mic.frames(), self.vad, self.cfg
            ):
                if self._fatal:
                    break
                t_captured = time.monotonic()
                if self._speech_check is not None and not await asyncio.to_thread(
                    self._speech_check, utterance, self.mic.sample_rate
                ):
                    # Typing, taps, a cough — the capture VAD was fooled but
                    # the classifier wasn't. Drop SILENTLY (typing must not
                    # spam ignored-lines onto the screen). Exception: when
                    # the VOICE-LOCK is doing the rejecting, repeated drops
                    # mean the lock may be doubting its own owner — say so
                    # with the escape hatch (typed input bypasses the gate).
                    if getattr(self._speech_check, "locked", False) and getattr(
                        self._speech_check, "last_score", None
                    ) is not None:
                        self._lock_rejects += 1
                        if self._lock_rejects in (3, 10):
                            self.render.status_note(
                                "voice-lock keeps rejecting — if that's YOU, "
                                'type "team session" + Enter, or re-enroll '
                                "with t2m --enroll-voice"
                            )
                    if self.cfg.debug:
                        self.render.debug("(speech check: not speech — dropped)")
                    continue
                self._lock_rejects = 0
                if self._echo_gate is not None and not await asyncio.to_thread(
                    self._echo_gate.foreign, utterance, self.mic.sample_rate
                ):
                    # Speakers full duplex: the segmenter captured our own
                    # spoken confirmation ("Paused. Say wake up…") coming
                    # back off the speakers. Drop it silently — transcribing
                    # it would have the agent answering itself.
                    if self.cfg.debug:
                        self.render.debug("(my own echo — dropped)")
                    continue
                if self.cfg.debug and getattr(
                    self._speech_check, "last_score", None
                ) is not None:
                    self.render.debug(
                        f"(voice score {self._speech_check.last_score:+.2f})"
                    )
                raw = await self.stt.transcribe(utterance, self.mic.sample_rate)
                if self.cfg.debug:
                    self.render.debug(
                        f"[t] stt {time.monotonic() - t_captured:.2f}s"
                    )
                if raw and looks_hallucinated(raw):
                    # Non-speech audio (fan hum, ambient) hallucinates as
                    # looped words — often the agent's own vocabulary, because
                    # the STT is context-seeded with it. Never send those.
                    if self._paused:
                        # Asleep: noise gets no screen time at all.
                        if self.cfg.debug:
                            self.render.debug("(noise while paused — ignored)")
                    else:
                        self.render.noise_ignored()
                    raw = ""
                text = collapse_stutter(raw)
                if not text:
                    # While paused, an empty transcript prints NOTHING —
                    # "🎧 listening…" under a ⏸ was a lie (live-reported).
                    if not self._paused:
                        self._show_listening(nl=False)
                    continue
                await self._handle_user_text(text)
                if self._fatal:
                    break
        finally:
            typed_task.cancel()
            if assembler_task is not None:
                assembler_task.cancel()
            self.mic.stop()
            # Release the audio output device at session end. stop() is a no-op-safe
            # method on the real Speaker (and the fake), so this can't strand a handle.
            self.speaker.stop()
            await self.backend.close()
            # Restore the terminal LAST — a fancy renderer may own screen
            # state; Plain's close() is a no-op. Safe to call twice.
            self.render.close()

    async def _consume_turn(self, events: AsyncIterator[AgentEvent]) -> str | None:
        """Drive one agent turn: stream text to TTS, surface tools, end on result.

        Returns the transcript of a barge-in utterance if the user cut this
        turn off (full-duplex only), else None.
        """
        pending = ""
        speaking = False
        saw_token = False
        self._interrupted = False
        self._spoke_any = False  # unlike `speaking`, never reset by the gate
        self._spoken_texts = []
        # The monitor runs in BOTH duplex modes. In half-duplex the mic mutes
        # once the agent starts speaking, so the monitor naturally only hears
        # the "thinking" gap — where fresh speech is almost always the user
        # finishing a sentence the segmenter cut too early (continuation).
        # In full duplex (--barge-in) it additionally hears through playback.
        self._barge_task = asyncio.create_task(self._barge_monitor())
        self._start_speech_pipeline()
        self._saw_tool = False
        self._tool_count = 0
        self._last_audible = time.monotonic()
        # Tool calls announced early by the stream path, awaiting their
        # full-message detail (upgrade) twin. Drives the dedupe: one call,
        # one line — the old behavior printed and logged every call twice.
        announced: list[str] = []
        ticker: asyncio.Task[None] | None = None
        if self.cfg.working_ticks:
            ticker = asyncio.create_task(self._working_ticker())
        self.render.agent_begin()

        async for ev in events:
            if isinstance(ev, AssistantTextDelta):
                if not saw_token:
                    saw_token = True
                    if self.cfg.debug and self._t_sent:
                        self.render.debug(
                            f"[t] first-token "
                            f"{time.monotonic() - self._t_sent:.2f}s",
                            nl=True,
                        )
                self.render.agent_delta(ev.text)
                pending += ev.text
                pending, ready = _drain_sentences(pending)
                if not ready and not self._spoke_any and not self._interrupted:
                    # Nothing spoken yet this turn: start on the first clause
                    # instead of waiting out a long opening sentence — the
                    # first chunk is also shorter, so it renders faster.
                    pending, clause = _drain_first_clause(pending)
                    if clause is not None:
                        ready = [clause]
                if not ready:
                    # Run-on text (a count, a list) can go hundreds of chars
                    # with only commas — the sentence chunker would sit silent
                    # until TurnComplete then dump one monster chunk. Overflow
                    # at clause boundaries keeps the voice continuous.
                    pending, chunk = _drain_overflow(pending)
                    if chunk is not None:
                        ready = [chunk]
                for sentence in ready:
                    if self._interrupted:
                        continue  # user cut playback; keep text on screen only
                    speaking = self._begin_speaking(speaking)
                    # Flip at ENQUEUE: audio follows within ~a render, and a
                    # cut after this point means the user reacted to (or
                    # talked over) real agent speech, not a silent turn.
                    self._spoke_any = True
                    self._spoken_texts.append(sentence)
                    assert self._render_q is not None
                    await self._render_q.put(sentence)
            elif isinstance(ev, ThinkingDelta):
                if speaking and self.cfg.half_duplex:
                    speaking = await self._reopen_ears_for_gap()
                self.render.thinking(ev.text)
            elif isinstance(ev, ToolActivity):
                if speaking and self.cfg.half_duplex:
                    # THE deafness fix (live-reported: "it completely stops
                    # listening while it codes"): half-duplex muted the mic at
                    # the turn's FIRST spoken sentence and kept it muted to
                    # the END of the turn — which today can be a multi-minute
                    # build. A tool call means the prose paused: finish
                    # speaking what's queued, then hand the ears back for the
                    # gap. The next sentence re-mutes exactly as before.
                    speaking = await self._reopen_ears_for_gap()
                if ev.upgrade and ev.name in announced:
                    # Detail for a call the stream path already showed: print
                    # the arguments as a follow-on, log once (with detail),
                    # and do NOT count it again.
                    announced.remove(ev.name)
                    self.render.tool(
                        ev.name, ev.summary, body=ev.body, follow_on=True
                    )
                    if self.log:
                        self.log.tool(ev.name, ev.summary)
                else:
                    self.render.tool(ev.name, ev.summary, body=ev.body)
                    self._saw_tool = True
                    self._tool_count += 1
                    if ev.upgrade:
                        # Partials disabled (or version skew): the full-message
                        # event is the only announcement — log it directly.
                        if self.log:
                            self.log.tool(ev.name, ev.summary)
                    else:
                        # Log deferred to the upgrade twin, which carries the
                        # arguments (guaranteed on the current protocol).
                        announced.append(ev.name)
            elif isinstance(ev, PermissionRequest):
                # The backend is paused awaiting our answer; run the spoken
                # approve/deny round-trip. Resets `speaking` so the next
                # sentence re-mutes the mic in half-duplex mode (the gate
                # unmuted it to listen).
                await self._handle_permission(ev)
                speaking = False
            elif isinstance(ev, TurnComplete):
                if pending.strip() and not self._interrupted:
                    speaking = self._begin_speaking(speaking)
                    self._spoke_any = True
                    self._spoken_texts.append(pending)
                    assert self._render_q is not None
                    await self._render_q.put(pending)
                # Feed proper nouns / identifiers from the agent's reply to the
                # STT as live hotword context — terms the user is likely to say
                # back next turn. Duck-typed: engines without set_context are
                # simply not seeded.
                if ev.text and hasattr(self.stt, "set_context"):
                    self.stt.set_context(_context_terms(ev.text))
                if self.log:
                    self.log.assistant(ev.text)
                self.render.agent_end()
                break
            elif isinstance(ev, SessionReady):
                continue
            elif isinstance(ev, BackendError):
                # Backend process is gone. Don't loop forever against a corpse —
                # flag it so run() breaks the listen loop into its finally block.
                self.render.error(f"[backend error] {ev.message}")
                self._fatal = True
                break

        if ticker is not None:
            ticker.cancel()
        # Let queued speech finish (or flush fast after an interrupt) BEFORE
        # the mic reopens — in half-duplex the agent must not be mid-sentence
        # with a live mic.
        await self._drain_speech_pipeline()

        if speaking:
            self.mic.set_muted(False)
            self.vad.reset()

        # A cut BEFORE any speech means the user never heard anything to react
        # to — they were finishing their own sentence. run() stitches it onto
        # the previous turn instead of sending a context-free fragment.
        self._continuation = self._interrupted and not self._spoke_any
        return await self._reap_barge_monitor()

    async def _handle_user_text(self, text: str, *, typed: bool = False) -> None:
        """One user turn, voice or typed, start to finish: control words,
        pause state, sending, the barge/continuation loop, the end-of-turn
        state line. Serialized by _turn_lock so voice and keyboard can never
        drive the backend at the same time. Typed turns mute the mic for
        their duration — the frame invariant (one consumer of mic audio per
        turn) survives, at the cost of voice barge-in during typed turns."""
        async with self._turn_lock:
            if self._fatal:
                return
            if typed:
                self.mic.set_muted(True)
            try:
                await self._run_user_turn(text, typed=typed)
            finally:
                if typed and not self._paused:
                    self.mic.set_muted(False)
                    self.vad.reset()

    async def _run_user_turn(self, text: str, *, typed: bool) -> None:
        # Controls first: "pause"/"wake up" work identically typed or spoken.
        intent = control_intent(text)
        if self._paused:
            if intent == "resume":
                await self._set_paused(False)
                self._show_listening()
            else:
                if self.cfg.debug:
                    self.render.paused_ignored(text)
                self._listening_fresh = False
                self.render.still_paused()
            return
        if intent == "pause":
            await self._set_paused(True)
            return
        if intent == "sessions":
            await self._session_picker()
            return
        if intent in ("team", "solo"):
            await self._set_voice_lock(intent == "solo")
            return
        if not typed:
            # An unfinished-sounding transcript is NOT sent yet: keep the
            # mic open and stitch the rest on BEFORE the agent ever sees a
            # fragment. Typed text is exact — no stitching.
            text = await self._extend_unfinished(text)
        self._listening_fresh = False
        self.render.you(text, "typed" if typed else "")
        if self.log:
            self.log.user(text, "typed" if typed else "")
        self._last_text = text
        if not self._titled:
            # First instruction = the session's handle in the spoken picker.
            self._titled = True
            sid = getattr(self.backend, "_session_id", None)
            if sid:
                record_title(self.cfg.cwd, sid, text)
        self._t_sent = time.monotonic()
        try:
            await self.backend.send(text)
        except (BrokenPipeError, ConnectionError, RuntimeError, OSError) as e:
            # claude died after we started a turn: stdin is closed, so the
            # write raises (or events would never arrive). Don't crash with
            # a traceback or hang forever — flag fatal and exit cleanly.
            self.render.error(f"[backend send failed] {e}")
            self._fatal = True
            return
        barge = await self._consume_turn(self._events)
        # A barge-in already contains the user's next utterance — send it
        # straight through instead of going back to listening. A cut that
        # landed before the agent spoke is a CONTINUATION: the segmenter
        # ended the user's turn mid-sentence and they kept going, so stitch
        # the fragments back into one instruction.
        noise_resends = 0
        control_resends = 0
        while not self._fatal:
            if barge:
                # Control words arriving through the barge path ("Sleep."
                # spoken while the agent worked) must NEVER reach the agent
                # as a message — live sessions showed Claude replying to
                # them. Flip the ears instead, and since the cut already
                # killed the turn: if it was mid-WORK, resend the task so
                # pause never cancels a job. Checked before continuation
                # stitching so a control word can't be glued onto the
                # previous turn.
                intent = control_intent(barge)
                if intent == "sessions":
                    # Never swap conversations out from under running work.
                    self.render.status_note(
                        "can't switch sessions mid-task — say it while "
                        "I'm listening"
                    )
                    break
                if intent is not None:
                    if intent in ("team", "solo"):
                        # Lock switch mid-work: flip silently and resume the
                        # interrupted task via the same never-cancel logic.
                        await self._set_voice_lock(
                            intent == "solo", spoken=False
                        )
                    else:
                        await self._set_paused(intent == "pause")
                    # Pause must never cancel work. "Work" = the turn ran
                    # tools OR hadn't spoken yet (cut mid-thinking — the
                    # answer never reached the user; live bug killed a build
                    # in its thinking phase). The one non-resend case: it was
                    # mid-SPEECH with no tools — that pause means "shut up",
                    # and resending would make it start talking again.
                    interrupted_work = self._saw_tool or not self._spoke_any
                    if (
                        interrupted_work
                        and self._last_text
                        and control_resends < 1
                    ):
                        control_resends += 1
                        to_send = self._last_text
                        self.render.status_note("resuming the interrupted task")
                    else:
                        break
                elif self._own_speech_echo(barge):
                    # The gate let a false cut through, and the "user
                    # message" transcribed to the agent's own words. Never
                    # send it — the agent answering itself is the one
                    # failure that makes the whole loop feel insane. The
                    # cut already happened; resume killed WORK, otherwise
                    # just go back to listening (the answer was already
                    # mostly delivered).
                    self.render.status_note(
                        "(that was my own voice off the speakers — ignoring)"
                    )
                    if self.log:
                        self.log.event(f"echo transcript swallowed: {barge!r}")
                    interrupted_work = self._saw_tool or not self._spoke_any
                    if (
                        interrupted_work
                        and self._last_text
                        and noise_resends < 1
                    ):
                        noise_resends += 1
                        to_send = self._last_text
                        self.render.status_note("resuming the interrupted task")
                    else:
                        break
                else:
                    # A barge can trail off exactly like a normal turn —
                    # give it the same stitch-before-send treatment so a
                    # fragment never reaches the agent through this path.
                    barge = await self._extend_unfinished(barge)
                    self._listening_fresh = False
                    if self._continuation and self._last_text:
                        to_send = f"{self._last_text} {barge}"
                        self.render.you(to_send, "continued")
                        if self.log:
                            self.log.user(to_send, "continued")
                    else:
                        to_send = barge
                        self.render.you(to_send, "barge-in")
                        if self.log:
                            self.log.user(to_send, "barge-in")
                    self._last_text = to_send
            elif self._continuation and self._last_text and noise_resends < 1:
                # A cut killed the turn before ANY answer but the captured
                # audio transcribed to nothing — a noise trigger. Don't let
                # it eat the user's question: resend it (once — a noisy
                # room must not loop forever).
                noise_resends += 1
                to_send = self._last_text
                self.render.noise_resend()
            else:
                break
            self._t_sent = time.monotonic()
            try:
                await self.backend.send(to_send)
            except (BrokenPipeError, ConnectionError, RuntimeError, OSError) as e:
                self.render.error(f"[backend send failed] {e}")
                self._fatal = True
                break
            barge = await self._consume_turn(self._events)
        if self._fatal:
            self.render.error("backend gone — shutting down.")
            return
        if self._paused:
            # A task that finished while asleep must not claim the ears are
            # open — remind how to wake instead.
            self._listening_fresh = False
            self.render.still_paused()
        else:
            self._show_listening()

    async def _session_picker(self) -> None:
        """Spoken 'resume previous session': list this folder's earlier
        sessions by their first instruction, hear a number, switch the live
        backend onto that conversation."""
        current = getattr(self.backend, "_session_id", None)
        sessions = list_sessions(self.cfg.cwd, exclude=current)
        if not sessions:
            self.render.status_note("no earlier sessions for this folder")
            await self._speak_confirm(
                "There are no earlier sessions for this folder."
            )
            return
        if not hasattr(self.backend, "switch_session"):
            self.render.status_note("this backend can't switch sessions")
            return
        self._listening_fresh = False
        self.render.status_note("earlier sessions here — say a number, or cancel:")
        for i, s in enumerate(sessions, 1):
            when = f"  ({s['started']})" if s.get("started") else ""
            self.render.status_note(f"  {i} · {s['title']}{when}")
        spoken = ". ".join(
            f"{i}: {s['title']}" for i, s in enumerate(sessions[:3], 1)
        )
        if self.cfg.half_duplex:
            self.mic.set_muted(True)
        await self._speak(
            f"Earlier sessions. {spoken}. Say a number, or cancel."
        )
        self.mic.set_muted(False)
        self.vad.reset()

        pick: int | None = None
        for attempt in range(2):
            heard = await self._listen_once(PERMISSION_LISTEN_TIMEOUT_S)
            words = re.findall(r"[a-z0-9]+", heard.lower())
            normalized = " ".join(words)
            if not heard:
                continue
            if normalized in _PICK_CANCEL or "cancel" in words:
                self.render.status_note("okay — staying here")
                await self._speak_confirm("Okay, staying here.")
                return
            for w in words:
                n = _PICK_WORDS.get(w)
                if n is not None and 1 <= n <= len(sessions):
                    pick = n
                    break
            if pick is not None:
                break
            if attempt == 0:
                await self._speak_confirm(
                    f"Say a number from one to {len(sessions)}, or cancel."
                )
        if pick is None:
            self.render.status_note("didn't catch a number — staying here")
            await self._speak_confirm("Didn't catch that — staying here.")
            return

        chosen = sessions[pick - 1]
        self.render.status_note(f"switching to: {chosen['title']}")
        await self.backend.switch_session(chosen["id"])
        save_last_session(self.cfg.cwd, chosen["id"])
        self._titled = True  # the resumed session already has its title
        self._last_text = ""
        await self._speak_confirm(f"Resumed: {chosen['title']}")
        self._show_listening()

    async def _typed_worker(self) -> None:
        """Consume assembled keyboard messages and run them as user turns."""
        try:
            while True:
                text = (await self._typed_queue.get()).strip()
                if text:
                    await self._handle_user_text(text, typed=True)
        except asyncio.CancelledError:
            raise

    async def _stdin_assembler(self) -> None:
        """Join a burst of stdin lines (a paste) into ONE message: lines that
        arrive within 80ms of each other belong together."""
        try:
            while True:
                parts = [await self._stdin_lines.get()]
                while True:
                    try:
                        parts.append(
                            await asyncio.wait_for(self._stdin_lines.get(), 0.08)
                        )
                    except asyncio.TimeoutError:
                        break
                # Join on newline, not "": the 80ms window exists to
                # reassemble a multi-line PASTE into one message — but each
                # readline() already strips nothing, so a bare join mashed
                # separate quick lines together ("he" + "idk…" -> "heidk",
                # live 2026-07-20). Line boundaries are content.
                text = "\n".join(p.strip() for p in parts).strip()
                if text:
                    await self._typed_queue.put(text)
        except asyncio.CancelledError:
            raise

    def _start_stdin_thread(self) -> None:
        """Pump terminal lines onto the loop — a dedicated daemon thread,
        like the Mic's PortAudio callback (to_thread would orphan a blocked
        readline on every timeout and eat the next typed line)."""
        loop = asyncio.get_running_loop()

        def pump() -> None:
            while True:
                try:
                    line = sys.stdin.readline()
                except (ValueError, OSError):
                    return  # stdin closed
                if not line:
                    return  # EOF
                loop.call_soon_threadsafe(self._stdin_lines.put_nowait, line)

        threading.Thread(target=pump, daemon=True, name="t2m-stdin").start()

    def _show_listening(self, *, nl: bool = True) -> None:
        """Print 🎧 once per quiet stretch — noise utterances used to reprint
        it a dozen times in a row (live-observed)."""
        if self._listening_fresh and not nl:
            return
        self.render.listening(nl=nl)
        self._listening_fresh = True

    async def _set_voice_lock(self, locked: bool, *, spoken: bool = True) -> None:
        """Flip solo/team mode on the speech gate (duck-typed)."""
        gate = self._speech_check
        if gate is None or not hasattr(gate, "set_locked") or getattr(
            gate, "voicelock", None
        ) is None:
            self.render.status_note(
                "voice-lock isn't enrolled — run t2m --enroll-voice"
            )
            return
        gate.set_locked(locked)
        self._listening_fresh = False
        if locked:
            self.render.status_note("solo session — locked to your voice")
            if self.log:
                self.log.event("voice-lock: solo session")
            if spoken:
                await self._speak_confirm("Locked to your voice.")
        else:
            self.render.status_note("team session — everyone can talk")
            if self.log:
                self.log.event("voice-lock: team session")
            if spoken:
                await self._speak_confirm("Team session. Everyone can talk.")

    async def _set_paused(self, paused: bool) -> None:
        """Flip the ears, with the matching chip + spoken confirmation."""
        self._paused = paused
        self._listening_fresh = False
        if paused:
            self.render.paused()
            if self.log:
                self.log.event("listening paused by voice")
            await self._speak_confirm("Paused. Say wake up when you need me.")
        else:
            self.render.awake()
            if self.log:
                self.log.event("listening resumed by voice")
            await self._speak_confirm("I'm back.")

    async def _reap_barge_monitor(self) -> str | None:
        """Collect the barge-in transcript (waiting out the utterance if the
        user is still mid-sentence), or cancel the monitor on a clean turn."""
        task, self._barge_task = self._barge_task, None
        if task is None:
            return None
        if not self._interrupted:
            task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            # Only swallow OUR OWN monitor-cancel. If the session itself is
            # being cancelled (Ctrl-C / Ctrl-T), swallowing here eats the
            # one-shot cancellation and the session RESURRECTS — live bug
            # 2026-07-19: the first Ctrl-C printed '🎧 listening…' and kept
            # running instead of quitting.
            current = asyncio.current_task()
            if current is not None and getattr(current, "cancelling", None):
                if current.cancelling():
                    raise
            return None

    async def _working_ticker(self) -> None:
        """Soft blip every WORKING_TICK_QUIET_S of silence during a tool run —
        the audible version of a spinner, so 'thinking hard' never sounds like
        'crashed'. Only after this turn has actually used a tool."""
        try:
            while True:
                await asyncio.sleep(2)
                if self._interrupted or not self._saw_tool:
                    continue
                if time.monotonic() - self._last_audible < WORKING_TICK_QUIET_S:
                    continue
                self._last_audible = time.monotonic()
                # Screen companion to the audible tick — a long silent build
                # shows life in BOTH channels.
                self.render.working(self._tool_count)
                if self._ticks_broken:
                    continue
                try:
                    subprocess.Popen(
                        ["afplay", "-v", "0.4", _TICK_SOUND],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except (FileNotFoundError, OSError):
                    self._ticks_broken = True  # no afplay (Linux) — screen only
        except asyncio.CancelledError:
            raise

    async def _barge_monitor(self) -> str | None:
        """Watch the live mic during agent speech; cut playback on real speech.

        Onset (min_speech_ms of consecutive voiced frames) -> stop the Speaker
        and interrupt the backend, then keep buffering until silence_ms of
        trailing silence and transcribe the whole interruption — including the
        onset audio, so the first word isn't clipped. Returns the transcript
        (or None if the stream ended before speech).
        """
        frame_ms = (self.vad.frame_samples / self.vad.sample_rate) * 1000.0
        onset_ms = max(self.cfg.min_speech_ms, BARGE_ONSET_MIN_MS)
        if getattr(self.cfg, "echo_guard", False):
            # Echo-guarded speakers: the pre-cut identity check needs >=0.8s
            # of audio to be reliable (shorter clips pass the lock
            # unchecked, which here would mean cutting on its own echo).
            onset_ms = max(onset_ms, 900)
        if self._echo_gate is not None:
            # Echo-gated speakers: a longer onset gives the gate more
            # envelope structure per verdict — live false cuts clustered on
            # short windows over dynamic prose.
            onset_ms = max(onset_ms, 650)
        onset_needed = max(1, int(onset_ms / frame_ms))
        silence_needed = max(1, int(self.cfg.silence_ms / frame_ms))
        cap_ms = (
            min(self.cfg.max_utterance_ms, BARGE_MAX_UTTERANCE_MS)
            if self.cfg.max_utterance_ms > 0
            else BARGE_MAX_UTTERANCE_MS
        )
        max_frames = max(1, int(cap_ms / frame_ms))
        pre_roll_frames = (
            max(1, int(self.cfg.pre_roll_ms / frame_ms))
            if self.cfg.pre_roll_ms > 0
            else 0
        )
        preroll: deque = deque(maxlen=pre_roll_frames or 1)
        self.vad.reset()
        buf: list = []
        consecutive = 0
        trailing = 0
        cut = False
        # Wake-word listener state, used ONLY while paused: collects
        # utterances WITHOUT ever cutting the turn, and acts solely on
        # resume commands. This is what keeps "wake up" audible mid-task
        # after a mid-task "sleep" — previously the monitor went fully deaf
        # while paused and wake words were swallowed (live-reported).
        wake_onset = max(1, int(self.cfg.min_speech_ms / frame_ms))
        w_buf: list = []
        w_voiced = 0
        w_trailing = 0
        w_started = False
        # Absolute wall-clock ceiling on the post-cut collection, set at the
        # cut. The frame-count cap bounds the same thing ONLY while frames
        # flow; if the pipeline ever starves (live 2026-07-19: a blocked
        # PortAudio abort froze the loop and '[barge-in] listening…' wedged
        # for minutes), this deadline flushes what was captured instead of
        # hanging the session.
        deadline = 0.0
        # Echo-gated speakers need TWO consecutive foreign verdicts before a
        # cut: field logs (2026-07-19) show own-echo scores flickering near
        # the bar (0.30, 0.41 on prose), while a real talk-over sustains
        # high. The second look, ~300ms later, sees a longer window — more
        # envelope structure — and echo transients don't survive it.
        confirm_frames = max(1, int(300 / frame_ms))
        check_at = onset_needed
        foreign_streak = 0

        frames = self.mic.frames()
        try:
            while True:
                if cut:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break  # starved or over-long: flush what we have
                    try:
                        frame = await asyncio.wait_for(anext(frames), remaining)
                    except (StopAsyncIteration, asyncio.TimeoutError):
                        break
                else:
                    try:
                        frame = await anext(frames)
                    except StopAsyncIteration:
                        break
                if self._paused and not cut:
                    speech = self.vad.is_speech(frame)
                    if not w_started:
                        if speech:
                            w_buf.append(frame)
                            w_voiced += 1
                            if w_voiced >= wake_onset:
                                w_started = True
                        else:
                            w_buf.clear()
                            w_voiced = 0
                        continue
                    w_buf.append(frame)
                    if speech:
                        w_trailing = 0
                    else:
                        w_trailing += 1
                    if w_trailing >= silence_needed or len(w_buf) >= max_frames:
                        utterance = np.concatenate(w_buf).astype(np.float32)
                        w_buf, w_voiced, w_trailing, w_started = [], 0, 0, False
                        if self._speech_check is not None and not await asyncio.to_thread(
                            self._speech_check, utterance, self.mic.sample_rate
                        ):
                            continue
                        raw = await self.stt.transcribe(
                            utterance, self.mic.sample_rate
                        )
                        if control_intent(collapse_stutter(raw)) == "resume":
                            # Wake WITHOUT touching the running turn: screen
                            # confirm only — a spoken one would talk over the
                            # agent. Barge re-arms naturally (paused is False).
                            self._paused = False
                            self._listening_fresh = False
                            self.render.awake()
                            self.render.status_note(
                                "still working — I'm listening again"
                            )
                            if self.log:
                                self.log.event(
                                    "listening resumed by voice (mid-task)"
                                )
                        elif self.cfg.debug and raw:
                            self.render.debug(f"(paused — ignored: {raw})")
                    continue
                speech = self.vad.is_speech(frame)
                if not cut:
                    if speech:
                        if consecutive == 0 and pre_roll_frames and preroll:
                            # Prepend the just-before-onset audio so the first
                            # word of the interruption isn't clipped.
                            buf.extend(preroll)
                            preroll.clear()
                        buf.append(frame)
                        consecutive += 1
                        if consecutive >= check_at:
                            # Last gate before the point of no return: cutting
                            # kills the agent's generation, so the onset audio
                            # must be CONFIRMED speech — typing and coughs fool
                            # the frame VAD but not the classifier. (Model is
                            # pre-warmed at startup; this costs ~a millisecond.)
                            if self._speech_check is not None:
                                onset_audio = np.concatenate(buf).astype(np.float32)
                                if not await asyncio.to_thread(
                                    self._speech_check,
                                    onset_audio,
                                    self.mic.sample_rate,
                                ):
                                    if self.cfg.debug:
                                        self.render.debug(
                                            "(barge onset rejected — not speech)"
                                        )
                                    buf.clear()
                                    consecutive = 0
                                    check_at = onset_needed
                                    foreign_streak = 0
                                    continue
                            if self._echo_gate is not None:
                                # Speakers full duplex: its own voice off the
                                # speaker IS speech and passes the classifier —
                                # the echo gate is what tells "me talking back"
                                # from "someone talking over me". Not foreign =
                                # keep playing; the window re-arms so a real
                                # talk-over still cuts within ~an onset.
                                onset_audio = np.concatenate(buf).astype(np.float32)
                                if not await asyncio.to_thread(
                                    self._echo_gate.foreign,
                                    onset_audio,
                                    self.mic.sample_rate,
                                ):
                                    if self.cfg.debug:
                                        res = getattr(
                                            self._echo_gate, "last_residual", None
                                        )
                                        lb = getattr(
                                            self._echo_gate, "last_lowband", None
                                        )
                                        tag = (
                                            f" {res:.2f}" if res is not None else ""
                                        )
                                        if lb is not None:
                                            tag += f" · low {lb:.2f}"
                                        self.render.debug(
                                            "(barge onset rejected — my own "
                                            f"echo{tag})"
                                        )
                                    buf.clear()
                                    consecutive = 0
                                    check_at = onset_needed
                                    foreign_streak = 0
                                    continue
                                foreign_streak += 1
                                if foreign_streak < 2:
                                    # First foreign verdict: hold fire and
                                    # re-judge on a longer window — echo
                                    # scores flicker, real talk-over holds.
                                    check_at = consecutive + confirm_frames
                                    continue
                            cut = True
                            deadline = (
                                time.monotonic() + cap_ms / 1000.0 + 2.0
                            )
                            self._interrupted = True
                            self.speaker.stop()
                            if self._echo_gate is not None:
                                # Field record of every cut's foreign score —
                                # threshold tuning runs on real numbers from
                                # normal sessions, not on lab guesses.
                                res = getattr(
                                    self._echo_gate, "last_residual", None
                                )
                                lb = getattr(
                                    self._echo_gate, "last_lowband", None
                                )
                                score = (
                                    f"foreign {res:.2f}"
                                    if res is not None
                                    else "foreign n/a"
                                )
                                if lb is not None:
                                    score += f" · low {lb:.2f}"
                                if self.log:
                                    self.log.event(f"barge cut ({score})")
                                if self.cfg.debug:
                                    self.render.debug(f"(cut — {score})")
                            await self.backend.interrupt()
                            self.render.barge_label(self._spoke_any)
                    else:
                        if pre_roll_frames:
                            # A near-miss blip: keep its audio in the window too.
                            preroll.extend(buf)
                            preroll.append(frame)
                        buf.clear()
                        consecutive = 0
                        check_at = onset_needed
                        foreign_streak = 0
                    continue
                buf.append(frame)
                if speech:
                    trailing = 0
                else:
                    trailing += 1
                    if trailing >= silence_needed:
                        break
                if max_frames and len(buf) >= max_frames:
                    break
        finally:
            await frames.aclose()

        if not cut or not buf:
            return None
        utterance = np.concatenate(buf).astype(np.float32)
        if self._speech_check is not None and not await asyncio.to_thread(
            self._speech_check, utterance, self.mic.sample_rate
        ):
            # The onset passed the gate but the full collection is noise —
            # None routes into the repeat-your-question recovery upstream.
            return None
        raw = await self.stt.transcribe(utterance, self.mic.sample_rate)
        if raw and looks_hallucinated(raw):
            # A noise-triggered cut with a hallucinated transcript: returning
            # None routes into the repeat-your-question recovery upstream.
            return None
        return collapse_stutter(raw) or None

    def _own_speech_echo(self, text: str) -> bool:
        """True when a barge transcript is a (fuzzy) fragment of what the
        agent itself said this turn — the echo-transcript backstop. Armed
        in BOTH speakers echo layers: the gate needs it for leaks, and
        native AEC keeps it as the belt against imperfect cancellation at
        max volume. STT mishears the echo ('which are commonest' for
        'which are common this…'), so the match is similarity over a
        sliding window, not equality."""
        armed = self._echo_gate is not None or (
            getattr(self.cfg, "aec_layer", "") == "native"
        )
        if not armed or not self._spoken_texts:
            return False
        b = _norm_speech(text)
        if len(b.split()) < 3:
            # One- or two-word fragments are too ambiguous to swallow — a
            # real "stop" must survive even if the agent just said "stop".
            return False
        s = _norm_speech(" ".join(self._spoken_texts))[-600:]
        if not s:
            return False
        if b in s:
            return True
        from difflib import SequenceMatcher

        # Window ≈ fragment length: a much longer window dilutes the ratio
        # (missed 'which are commonest' vs 'which are common this…' live).
        win = len(b) + 4
        best = 0.0
        for i in range(0, max(1, len(s) - len(b) + 1), 4):
            best = max(best, SequenceMatcher(None, b, s[i : i + win]).ratio())
            if best >= 0.72:
                return True
        return False

    async def _reopen_ears_for_gap(self) -> bool:
        """Half-duplex only: the agent went quiet (tools/thinking) — let the
        queued speech finish, restart the pipeline for later prose, and
        unmute the mic so the user is heard DURING the work. Returns the new
        `speaking` state (False)."""
        await self._drain_speech_pipeline()
        self._start_speech_pipeline()
        self.mic.set_muted(False)
        self.vad.reset()
        return False

    def _begin_speaking(self, speaking: bool) -> bool:
        """Mark the start of agent speech, muting the mic in half-duplex mode.

        Half-duplex (the default) mutes the mic so the agent's own voice can't
        retrigger the VAD. Returns the updated `speaking` flag.
        """
        if speaking:
            return True
        if self.cfg.half_duplex:
            self.mic.set_muted(True)
        # Full duplex: the mic stays live — the barge-in monitor started by
        # _consume_turn owns interruption. Headphones are assumed (the mic
        # never hears the TTS), which is what makes this work without acoustic
        # echo cancellation.
        return True

    async def _speak(self, text: str) -> None:
        """Direct, serial speech — used by the permission gate's prompts. Turn
        prose goes through the render/play pipeline instead."""
        await self.speaker.play(self.tts.synthesize(text))

    async def _speak_confirm(self, text: str) -> None:
        """Speak a short status line with the half-duplex mute held, so the
        confirmation itself can't retrigger the ears."""
        if self.cfg.half_duplex:
            self.mic.set_muted(True)
        await self._speak(text)
        self.mic.set_muted(False)
        self.vad.reset()

    # ---- speech pipeline -------------------------------------------------

    def _start_speech_pipeline(self) -> None:
        self._render_q = asyncio.Queue()
        # maxsize=1: render exactly one chunk ahead of playback.
        self._blocks_q = asyncio.Queue(maxsize=1)
        self._render_task = asyncio.create_task(self._render_worker())
        self._play_task = asyncio.create_task(self._play_worker())

    async def _drain_speech_pipeline(self) -> None:
        """Finish queued speech and stop the workers. Safe to call twice."""
        if self._render_task is None or self._render_q is None:
            return
        await self._render_q.put(None)
        # NOT gather(return_exceptions=True): that swallows KeyboardInterrupt
        # raised inside a worker, eating the user's first Ctrl-C (live bug).
        # Worker Exceptions are already handled inside the workers; anything
        # else (KI, cancellation) must propagate.
        for task in (self._render_task, self._play_task):
            if task is not None:
                try:
                    await task
                except Exception:
                    pass
        self._render_task = self._play_task = None
        self._render_q = self._blocks_q = None

    async def _render_worker(self) -> None:
        assert self._render_q is not None and self._blocks_q is not None
        while True:
            text = await self._render_q.get()
            if text is None:
                await self._blocks_q.put(None)
                return
            if self._interrupted:
                continue
            blocks: list = []
            try:
                async for block in self.tts.synthesize(text):
                    blocks.append(block)
            except Exception as exc:  # a bad render skips the chunk, not the turn
                self.render.error(f"[tts] {exc!r}")
                continue
            await self._blocks_q.put(blocks)

    async def _play_worker(self) -> None:
        assert self._blocks_q is not None
        first = True
        while True:
            item = await self._blocks_q.get()
            if item is None:
                return
            if self._interrupted:
                continue
            if first:
                first = False
                if self.cfg.debug and self._t_sent:
                    self.render.debug(
                        f"[t] first-audio "
                        f"{time.monotonic() - self._t_sent:.2f}s",
                        nl=True,
                    )
            self._last_audible = time.monotonic()
            await self.speaker.play(_iter_blocks(item))
            self._last_audible = time.monotonic()

    async def _handle_permission(self, ev: PermissionRequest) -> None:
        """Spoken approve/deny gate for one paused tool call.

        Speak a short summary, hand the mic over for one utterance, match it
        against the intent grammar. Unclear -> re-ask once -> deny. The CLI
        blocks until respond_permission lands, so the turn simply resumes (or
        skips the tool) afterward.
        """
        detail = _permission_detail(ev)
        self.render.permission_ask(ev.tool_name, detail)
        # Let any queued turn speech finish before the spoken prompt, then
        # take over the audio path serially for the approval round-trip. The
        # pipeline restarts before returning so post-approval prose flows.
        await self._drain_speech_pipeline()
        # Full duplex: the barge-in monitor is also reading mic frames — park it
        # so the gate's listener doesn't race it for the approve/deny utterance.
        # (An approval prompt shouldn't be barge-able anyway.) _consume_turn
        # restarts nothing here: the monitor stays down for the rest of the
        # turn, which trades barge-in on post-approval speech for a race-free
        # gate — the next turn re-arms it.
        monitor, self._barge_task = self._barge_task, None
        if monitor is not None:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        decision: str | None = None
        for attempt in range(2):
            prompt = (
                _phrase_permission(ev)
                if attempt == 0
                else "I didn't catch that — approve or deny?"
            )
            if self.cfg.half_duplex:
                self.mic.set_muted(True)
            await self._speak(prompt)
            self.mic.set_muted(False)
            self.vad.reset()
            heard = await self._listen_once(PERMISSION_LISTEN_TIMEOUT_S)
            decision = match_intent(heard)
            if heard:
                self.render.permission_heard(heard, decision)
            if decision is not None:
                break
        allow = decision == "approve"
        self.render.permission_verdict(ev.tool_name, allow)
        await self.backend.respond_permission(
            ev.request_id, allow, message=None if allow else "Denied by voice"
        )
        if self.log:
            self.log.permission(ev.tool_name, detail, allow)
        self._start_speech_pipeline()

    async def _listen_once(self, onset_timeout: float) -> str:
        """Capture and transcribe a single utterance (gate + continuations).

        `onset_timeout` bounds only the wait for speech to START; a started
        utterance is always collected to its natural end (trailing silence /
        max-utterance cap) — a hard total-time cutoff cancelled sentences
        mid-air in live sessions. Reads the shared mic frame stream directly;
        safe because run()'s main segmenter is parked while this runs.
        Timeout or stream end without enough speech -> "".
        """
        frame_ms = (self.vad.frame_samples / self.vad.sample_rate) * 1000.0
        onset_needed = max(1, int(self.cfg.min_speech_ms / frame_ms))
        silence_needed = max(1, int(self.cfg.silence_ms / frame_ms))
        max_frames = (
            max(1, int(self.cfg.max_utterance_ms / frame_ms))
            if self.cfg.max_utterance_ms > 0
            else 0
        )
        pre_roll_frames = (
            max(1, int(self.cfg.pre_roll_ms / frame_ms))
            if self.cfg.pre_roll_ms > 0
            else 0
        )
        preroll: deque = deque(maxlen=pre_roll_frames or 1)
        deadline = time.monotonic() + onset_timeout
        buf: list = []
        voiced = 0
        trailing = 0
        started = False

        self.vad.reset()
        frames = self.mic.frames()
        try:
            while True:
                if started:
                    try:
                        frame = await anext(frames)
                    except StopAsyncIteration:
                        break  # stream end mid-utterance: flush what we have
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ""
                    try:
                        frame = await asyncio.wait_for(anext(frames), remaining)
                    except (StopAsyncIteration, asyncio.TimeoutError):
                        return ""
                speech = self.vad.is_speech(frame)
                if not started:
                    if speech:
                        if voiced == 0 and pre_roll_frames and preroll:
                            buf.extend(preroll)
                            preroll.clear()
                        buf.append(frame)
                        voiced += 1
                        if voiced >= onset_needed:
                            started = True
                    else:
                        if pre_roll_frames:
                            preroll.extend(buf)
                            preroll.append(frame)
                        buf.clear()
                        voiced = 0
                    continue
                buf.append(frame)
                if speech:
                    trailing = 0
                else:
                    trailing += 1
                    if trailing >= silence_needed:
                        break
                if max_frames and len(buf) >= max_frames:
                    break
        finally:
            await frames.aclose()

        if not started or not buf:
            return ""
        utterance = np.concatenate(buf).astype(np.float32)
        if self._speech_check is not None and not await asyncio.to_thread(
            self._speech_check, utterance, self.mic.sample_rate
        ):
            # Typing during a continuation window or a permission prompt must
            # never be stitched into a sentence or read as a verdict.
            return ""
        if self._echo_gate is not None and not await asyncio.to_thread(
            self._echo_gate.foreign, utterance, self.mic.sample_rate
        ):
            # A late echo tail of the prompt itself must never read as an
            # approve/deny verdict or a session pick.
            return ""
        return collapse_stutter(
            await self.stt.transcribe(utterance, self.mic.sample_rate)
        )

    async def _extend_unfinished(self, text: str) -> str:
        """Keep the mic open while a transcript sounds unfinished, stitching
        follow-up fragments on BEFORE anything reaches the agent."""
        extensions = 0
        while _seems_unfinished(text) and extensions < MAX_CONTINUATION_EXTENSIONS:
            self.render.waiting_for_rest()
            more = await self._listen_once(CONTINUATION_WAIT_S)
            if not more:
                break
            text = f"{text} {more}"
            extensions += 1
        return text


def _drain_sentences(buf: str) -> tuple[str, list[str]]:
    """Pull complete sentences out of `buf`; return (remainder, [sentences]).

    Skips false sentence ends: abbreviations ("e.g.", "Dr.", "etc.") and
    decimals ("3.14"). A candidate terminator only splits when the word it
    follows isn't a known abbreviation and isn't a digit-period-digit run.
    """
    out: list[str] = []
    last = 0
    carry = ""  # text held back from a non-splitting terminator
    for m in _SENTENCE.finditer(buf):
        sentence = carry + m.group(1)
        if _is_false_terminator(buf, m):
            # Hold this fragment and fold it into the next real sentence,
            # keeping the inter-token whitespace the regex consumed.
            carry = sentence + m.group(2)
            continue
        out.append(sentence.strip())
        carry = ""
        last = m.end()
    return buf[last:], out


# First-clause boundary for the FIRST spoken chunk of a turn: a comma /
# semicolon / colon / dash after at least 24 chars (avoids "Hi," micro-chunks)
# and at most 90 (bounds the wait). Only used before anything has been spoken;
# later chunks use full sentences.
_FIRST_CLAUSE = re.compile(r"^(.{24,90}?[,;:—-])\s")


def _drain_first_clause(buf: str) -> tuple[str, str | None]:
    """Split the first speakable clause off `buf`; (remainder, clause|None)."""
    m = _FIRST_CLAUSE.match(buf)
    if m is None:
        return buf, None
    return buf[m.end() :], m.group(1)


# Ceiling on unspoken buffered text before the overflow chunker fires. Run-on
# prose (counting, lists) has no sentence terminators, so without this the
# voice stalls mid-turn and then dumps everything at once.
_MAX_PENDING_CHARS = 160
_CLAUSE_BREAK = re.compile(r"[,;:]\s+")


def _drain_overflow(buf: str) -> tuple[str, str | None]:
    """Split a speakable chunk off an over-long sentence-less buffer.

    Prefers the LAST clause boundary inside the window; falls back to the last
    word break. Returns (remainder, chunk|None).
    """
    if len(buf) <= _MAX_PENDING_CHARS:
        return buf, None
    window = buf[:_MAX_PENDING_CHARS]
    last = None
    for m in _CLAUSE_BREAK.finditer(window):
        last = m
    if last is not None:
        return buf[last.end() :], window[: last.end()].rstrip()
    space = window.rfind(" ")
    if space <= 0:
        return buf, None
    return buf[space + 1 :], window[:space]


# Words that look like things a user would echo back: CamelCase / snake_case
# identifiers, dotted filenames, and Capitalized proper nouns (which we only
# keep when they're not sentence-initial dictionary words — cheap heuristic:
# length >= 4).
_CONTEXT_TERM = re.compile(
    r"[A-Za-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+"  # CamelCase / mixedCase
    r"|[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+"  # snake_case
    r"|[\w-]+\.[A-Za-z]{1,4}\b"  # file.ext
    r"|[A-Z][a-z]{3,}"  # Proper nouns
)


def _context_terms(text: str, limit: int = 30) -> list[str]:
    """Extract identifier-ish terms from agent prose for STT hotword seeding."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _CONTEXT_TERM.finditer(text):
        term = m.group(0)
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
            if len(out) >= limit:
                break
    return out


async def _iter_blocks(blocks: list):
    """Adapt a pre-rendered block list back to the async-iterable play() expects."""
    for block in blocks:
        yield block


# Whisper's known repetition-loop artifact: ambiguous audio transcribes as the
# same word dozens of times ("Okay. Okay. Okay. ×26", live-observed). Three
# consecutive repeats carry all the human meaning there is.
_STUTTER = re.compile(r"\b(\S+)((?:[\s.,!?]+\1\b){3,})", re.IGNORECASE)


def _norm_speech(text: str) -> str:
    """Lowercase alpha-numeric words, single-spaced — the comparison space
    for the echo-transcript backstop (punctuation and case are STT noise)."""
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def collapse_stutter(text: str) -> str:
    """Collapse >3 consecutive repeats of the same word down to three."""
    return _STUTTER.sub(lambda m: " ".join([m.group(1)] * 3), text)


def looks_hallucinated(raw: str) -> bool:
    """True for transcripts that are machine noise, not speech.

    Whisper hallucinates on non-speech audio (silence, TTS bleed, fan hum) —
    live-observed as "Okay." ×26 and "Building" ×10 while the user said
    nothing. Signature: a long utterance built from one or two unique words.
    Real speech with that shape ("no no no no no") is vanishingly rare past
    five words; deliberate short emphasis passes untouched.
    """
    words = re.findall(r"[a-z']+", raw.lower())
    return len(words) >= 5 and len(set(words)) <= 2


# Whole-utterance voice controls for the listening loop itself. Matched only
# when the ENTIRE utterance is the command (normalized), so "pause listening
# to him and focus" never triggers it.
_PAUSE_COMMANDS = frozenset(
    (
        "pause",
        "pause listening",
        "stop listening",
        "sleep",
        "go to sleep",
        "go to bed",
        "take a break",
    )
)
_RESUME_COMMANDS = frozenset(
    ("unpause", "wake up", "resume listening", "start listening", "im back")
)
# Voice-lock mode switches: "team session" opens the ears to everyone;
# "solo session" locks them back to the enrolled voice.
_TEAM_COMMANDS = frozenset(
    (
        "team session",
        "team mode",
        "everyone can talk",
        "listen to everyone",
        "unlock my voice",
        "voice lock off",
    )
)
_SOLO_COMMANDS = frozenset(
    (
        "solo session",
        "solo mode",
        "only me",
        "just me",
        "lock to my voice",
        "voice lock on",
    )
)

# Whole-utterance triggers for the spoken session picker.
_SESSION_COMMANDS = frozenset(
    (
        "resume previous session",
        "continue previous session",
        "resume a previous session",
        "continue a previous session",
        "resume last session",
        "continue last session",
        "resume session",
        "previous session",
        "list sessions",
        "session list",
        "continue where we left off",
        "resume where we left off",
    )
)

# Spoken pick -> list index (1-based). "cancel"-family aborts.
_PICK_WORDS = {
    "one": 1, "1": 1, "first": 1, "latest": 1, "last": 1,
    "two": 2, "2": 2, "second": 2,
    "three": 3, "3": 3, "third": 3,
    "four": 4, "4": 4, "fourth": 4,
    "five": 5, "5": 5, "fifth": 5,
}
_PICK_CANCEL = frozenset(("cancel", "never mind", "nevermind", "stop", "forget it"))


# Politeness and hesitation wrapped around a command must not defeat it:
# "Hey actually, pause." reached the AGENT (which cheerfully faked a pause —
# live-observed). Stripped from the ends only, so words inside a phrase and
# real instructions ("keep working") stay untouched.
_FILLER_WORDS = frozenset(
    "hey ok okay so um uh well please now wait actually alright yeah like".split()
)


def _single_control_intent(text: str) -> str | None:
    """Whole-phrase match with consecutive-stutter collapse ("Pause. Pause,
    Listening." still lands the command — live-observed) and end-filler
    stripping ("Hey actually, pause." / "pause please")."""
    words = re.findall(r"[a-z]+", text.lower().replace("'", ""))
    deduped = [w for i, w in enumerate(words) if i == 0 or w != words[i - 1]]
    while deduped and deduped[0] in _FILLER_WORDS:
        deduped.pop(0)
    while deduped and deduped[-1] in _FILLER_WORDS:
        deduped.pop()
    normalized = " ".join(deduped)
    if normalized in _PAUSE_COMMANDS:
        return "pause"
    if normalized in _RESUME_COMMANDS:
        return "resume"
    if normalized in _SESSION_COMMANDS:
        return "sessions"
    if normalized in _TEAM_COMMANDS:
        return "team"
    if normalized in _SOLO_COMMANDS:
        return "solo"
    return None


def control_intent(text: str) -> str | None:
    """Map an utterance to "pause" / "resume" / None.

    Two passes: the whole utterance first, then — because the STT often glues
    repeated commands into one line ("Sleep. pause", live-observed sailing
    straight to the agent) — sentence segments, matching only when EVERY
    segment carries the same intent. Mixed content ("Keep working. Sleep.")
    stays None: a real instruction must never be swallowed as a control word.
    """
    whole = _single_control_intent(text)
    if whole is not None:
        return whole
    segments = [s for s in re.split(r"[.!?;,]+", text) if s.strip()]
    if len(segments) < 2:
        return None
    intents = {_single_control_intent(s) for s in segments}
    if len(intents) == 1 and None not in intents:
        return intents.pop()
    return None


def match_intent(text: str) -> str | None:
    """Map a transcribed utterance to "approve" / "deny" / None (unclear).

    Deny tokens win over approve tokens ("no, don't do it" must deny), and an
    empty or unmatched utterance is None so the caller can re-ask — the gate
    never auto-allows on uncertainty.
    """
    normalized = text.lower()
    tokens = set(re.findall(r"[a-z']+", normalized))
    if tokens & _DENY_TOKENS:
        return "deny"
    if tokens & _APPROVE_TOKENS:
        return "approve"
    if any(phrase in normalized for phrase in _APPROVE_PHRASES):
        return "approve"
    return None


def _permission_detail(ev: PermissionRequest) -> str:
    """One transcript line of what the tool wants — eyes-on detail, not spoken."""
    inp = ev.tool_input or {}
    for key in ("command", "file_path", "url", "pattern"):
        if key in inp:
            return f"{key}={str(inp[key])[:160]}"
    return str(inp)[:160] if inp else "(no args)"


def _phrase_permission(ev: PermissionRequest) -> str:
    """Short, intent-bearing spoken summary of a permission request."""
    name = ev.tool_name
    inp = ev.tool_input or {}
    if name == "Bash":
        words = str(inp.get("command", "")).split()
        short = " ".join(words[:12]) + (" and more" if len(words) > 12 else "")
        action = f"run the command {short}" if short else "run a shell command"
    elif name == "Write":
        base = os.path.basename(str(inp.get("file_path", "")))
        action = f"create the file {base}" if base else "create a file"
    elif name == "Edit":
        base = os.path.basename(str(inp.get("file_path", "")))
        action = f"edit {base}" if base else "edit a file"
    elif name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 1 else "MCP"
        tool = parts[2] if len(parts) > 2 else "a tool"
        action = f"use the {server} tool {tool}"
    else:
        action = f"use {name}"
    return f"Claude wants to {action}. Approve or deny?"


def _is_false_terminator(buf: str, m: re.Match[str]) -> bool:
    """True when the terminator at match `m` is an abbreviation or a decimal."""
    chunk = m.group(1)
    # A '.' between two digits (e.g. "3.14") — only fires for single-dot ends.
    if chunk.endswith(".") and not chunk.endswith(".."):
        after = buf[m.end(1) : m.end(1) + 1]
        before = chunk[-2:-1]
        if before.isdigit() and after.isdigit():
            return True
    # Trailing word (letters/dots) right before the terminator, periods stripped.
    word = re.search(r"([A-Za-z][A-Za-z.]*)[.!?]+$", chunk)
    if word is None:
        return False
    token = word.group(1).rstrip(".").lower()
    return token in _ABBREVIATIONS

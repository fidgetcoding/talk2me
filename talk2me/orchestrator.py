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
import sys
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
    ToolActivity,
    TurnComplete,
)
from .protocols import STT, TTS, VAD, AgentBackend
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


# Sustained speech needed before the monitor CUTS a turn (barge/continuation).
# Deliberately higher than min_speech_ms: a cut kills the agent's turn, so a
# breath, chair squeak, or trailing word must not fire it — live sessions
# showed 250ms onsets false-triggering and eating turns. A real "okay stop"
# easily sustains 450ms.
BARGE_ONSET_MIN_MS = 450

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
    ) -> None:
        self.cfg = cfg
        self.backend = backend
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.mic = mic
        self.speaker = speaker
        # Set when a turn dies on a BackendError; tells run() to stop cleanly
        # instead of looping forever against a dead backend process.
        self._fatal = False
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
        # Per-turn render/play pipeline: sentence n+1 renders WHILE sentence n
        # plays, so the voice never stalls between chunks waiting on `say`.
        self._render_q: asyncio.Queue[str | None] | None = None
        self._blocks_q: asyncio.Queue[list | None] | None = None
        self._render_task: asyncio.Task[None] | None = None
        self._play_task: asyncio.Task[None] | None = None

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
            print("(loading the ears…)", flush=True)
            await asyncio.to_thread(self.stt.warmup)
        print("talk2me ready — start talking. Ctrl-C to quit.", flush=True)
        if self.cfg.half_duplex:
            print(
                "   (half-duplex: talking over the agent mid-speech is ignored "
                "— run with --barge-in and headphones to interrupt it)",
                flush=True,
            )
        try:
            print("\n🎧 listening…", flush=True)
            last_text = ""
            async for utterance in segment_utterances(
                self.mic.frames(), self.vad, self.cfg
            ):
                t_captured = time.monotonic()
                text = await self.stt.transcribe(utterance, self.mic.sample_rate)
                if self.cfg.debug:
                    print(
                        f"  [t] stt {time.monotonic() - t_captured:.2f}s",
                        flush=True,
                    )
                if not text:
                    print("🎧 listening…", flush=True)
                    continue
                # An unfinished-sounding transcript is NOT sent yet: keep the
                # mic open and stitch the rest on BEFORE the agent ever sees a
                # fragment ("So what do you call … [pause] … a linked list").
                text = await self._extend_unfinished(text)
                print(f"\n🗣  you: {text}", flush=True)
                last_text = text
                self._t_sent = time.monotonic()
                try:
                    await self.backend.send(text)
                except (BrokenPipeError, ConnectionError, RuntimeError, OSError) as e:
                    # claude died after we started a turn: stdin is closed, so the
                    # write raises (or events would never arrive). Don't crash with
                    # a traceback or hang forever — flag fatal and exit cleanly via
                    # the finally block. (The BackendError-event path sets _fatal too;
                    # this guards the send call itself.)
                    print(f"\n[backend send failed] {e}", flush=True)
                    self._fatal = True
                    break
                barge = await self._consume_turn(events)
                # A barge-in already contains the user's next utterance — send
                # it straight through instead of going back to listening. A cut
                # that landed before the agent spoke is a CONTINUATION: the
                # segmenter ended the user's turn mid-sentence and they kept
                # going, so stitch the fragments back into one instruction.
                noise_resends = 0
                while not self._fatal:
                    if barge:
                        # A barge can trail off exactly like a normal turn —
                        # give it the same stitch-before-send treatment so a
                        # fragment never reaches the agent through this path.
                        barge = await self._extend_unfinished(barge)
                        if self._continuation and last_text:
                            to_send = f"{last_text} {barge}"
                            print(f"\n🗣  you (continued): {to_send}", flush=True)
                        else:
                            to_send = barge
                            print(f"\n🗣  you (barge-in): {to_send}", flush=True)
                        last_text = to_send
                    elif self._continuation and last_text and noise_resends < 1:
                        # A cut killed the turn before ANY answer but the
                        # captured audio transcribed to nothing — a noise
                        # trigger. Don't let it eat the user's question: resend
                        # it (once — a noisy room must not loop forever).
                        noise_resends += 1
                        to_send = last_text
                        print(
                            "\n   (noise interrupt — repeating your question)",
                            flush=True,
                        )
                    else:
                        break
                    self._t_sent = time.monotonic()
                    try:
                        await self.backend.send(to_send)
                    except (BrokenPipeError, ConnectionError, RuntimeError, OSError) as e:
                        print(f"\n[backend send failed] {e}", flush=True)
                        self._fatal = True
                        break
                    barge = await self._consume_turn(events)
                if self._fatal:
                    print("\nbackend gone — shutting down.", flush=True)
                    break
                print("\n🎧 listening…", flush=True)
        finally:
            self.mic.stop()
            # Release the audio output device at session end. stop() is a no-op-safe
            # method on the real Speaker (and the fake), so this can't strand a handle.
            self.speaker.stop()
            await self.backend.close()

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
        # The monitor runs in BOTH duplex modes. In half-duplex the mic mutes
        # once the agent starts speaking, so the monitor naturally only hears
        # the "thinking" gap — where fresh speech is almost always the user
        # finishing a sentence the segmenter cut too early (continuation).
        # In full duplex (--barge-in) it additionally hears through playback.
        self._barge_task = asyncio.create_task(self._barge_monitor())
        self._start_speech_pipeline()
        sys.stdout.write("🤖 ")
        sys.stdout.flush()

        async for ev in events:
            if isinstance(ev, AssistantTextDelta):
                if not saw_token:
                    saw_token = True
                    if self.cfg.debug and self._t_sent:
                        print(
                            f"\n  [t] first-token "
                            f"{time.monotonic() - self._t_sent:.2f}s",
                            flush=True,
                        )
                sys.stdout.write(ev.text)
                sys.stdout.flush()
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
                    assert self._render_q is not None
                    await self._render_q.put(sentence)
            elif isinstance(ev, ToolActivity):
                print(f"\n   [tool] {ev.name}", flush=True)
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
                    assert self._render_q is not None
                    await self._render_q.put(pending)
                # Feed proper nouns / identifiers from the agent's reply to the
                # STT as live hotword context — terms the user is likely to say
                # back next turn. Duck-typed: engines without set_context are
                # simply not seeded.
                if ev.text and hasattr(self.stt, "set_context"):
                    self.stt.set_context(_context_terms(ev.text))
                print(flush=True)
                break
            elif isinstance(ev, SessionReady):
                continue
            elif isinstance(ev, BackendError):
                # Backend process is gone. Don't loop forever against a corpse —
                # flag it so run() breaks the listen loop into its finally block.
                print(f"\n[backend error] {ev.message}", flush=True)
                self._fatal = True
                break

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
            return None

    async def _barge_monitor(self) -> str | None:
        """Watch the live mic during agent speech; cut playback on real speech.

        Onset (min_speech_ms of consecutive voiced frames) -> stop the Speaker
        and interrupt the backend, then keep buffering until silence_ms of
        trailing silence and transcribe the whole interruption — including the
        onset audio, so the first word isn't clipped. Returns the transcript
        (or None if the stream ended before speech).
        """
        frame_ms = (self.vad.frame_samples / self.vad.sample_rate) * 1000.0
        onset_needed = max(
            1, int(max(self.cfg.min_speech_ms, BARGE_ONSET_MIN_MS) / frame_ms)
        )
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
        self.vad.reset()
        buf: list = []
        consecutive = 0
        trailing = 0
        cut = False

        async for frame in self.mic.frames():
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
                    if consecutive >= onset_needed:
                        cut = True
                        self._interrupted = True
                        self.speaker.stop()
                        await self.backend.interrupt()
                        label = (
                            "[barge-in] listening…"
                            if self._spoke_any
                            else "[go on…]"
                        )
                        print(f"\n   {label}", flush=True)
                else:
                    if pre_roll_frames:
                        # A near-miss blip: keep its audio in the window too.
                        preroll.extend(buf)
                        preroll.append(frame)
                    buf.clear()
                    consecutive = 0
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

        if not cut or not buf:
            return None
        utterance = np.concatenate(buf).astype(np.float32)
        text = await self.stt.transcribe(utterance, self.mic.sample_rate)
        return text or None

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
        await asyncio.gather(
            self._render_task, self._play_task, return_exceptions=True
        )
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
                print(f"\n[tts] {exc!r}", flush=True)
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
                    print(
                        f"\n  [t] first-audio "
                        f"{time.monotonic() - self._t_sent:.2f}s",
                        flush=True,
                    )
            await self.speaker.play(_iter_blocks(item))

    async def _handle_permission(self, ev: PermissionRequest) -> None:
        """Spoken approve/deny gate for one paused tool call.

        Speak a short summary, hand the mic over for one utterance, match it
        against the intent grammar. Unclear -> re-ask once -> deny. The CLI
        blocks until respond_permission lands, so the turn simply resumes (or
        skips the tool) afterward.
        """
        detail = _permission_detail(ev)
        print(f"\n   [permission] {ev.tool_name}: {detail}", flush=True)
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
                print(
                    f"   [permission] you: {heard} -> {decision or 'unclear'}",
                    flush=True,
                )
            if decision is not None:
                break
        allow = decision == "approve"
        print(
            f"   [permission] {'APPROVED' if allow else 'DENIED'}: {ev.tool_name}",
            flush=True,
        )
        await self.backend.respond_permission(
            ev.request_id, allow, message=None if allow else "Denied by voice"
        )
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
        return await self.stt.transcribe(utterance, self.mic.sample_rate)

    async def _extend_unfinished(self, text: str) -> str:
        """Keep the mic open while a transcript sounds unfinished, stitching
        follow-up fragments on BEFORE anything reaches the agent."""
        extensions = 0
        while _seems_unfinished(text) and extensions < MAX_CONTINUATION_EXTENSIONS:
            print("   (…waiting for the rest)", flush=True)
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

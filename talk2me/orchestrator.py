"""The turn-taking voice loop. Owns the conversation; depends only on Protocols.

Half-duplex flow (the default — no echo-cancellation hardware needed):

    listen -> segment one utterance -> transcribe -> send to agent
           -> stream agent text, speaking sentence-by-sentence (mic muted)
           -> hand the mic back, listen again

Barge-in / full duplex keeps the mic live during playback and stops the Speaker
when fresh speech is detected. That path is gated behind cfg.barge_in and needs
echo handling; half-duplex is what runs today.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncIterator

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

    async def run(self) -> None:
        await self.backend.start()
        self.mic.start()
        events = self.backend.events()
        print("talk2me ready — start talking. Ctrl-C to quit.", flush=True)
        try:
            print("\n🎧 listening…", flush=True)
            async for utterance in segment_utterances(
                self.mic.frames(), self.vad, self.cfg
            ):
                text = await self.stt.transcribe(utterance, self.mic.sample_rate)
                if not text:
                    print("🎧 listening…", flush=True)
                    continue
                print(f"\n🗣  you: {text}", flush=True)
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
                await self._consume_turn(events)
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

    async def _consume_turn(self, events: AsyncIterator[AgentEvent]) -> None:
        """Drive one agent turn: stream text to TTS, surface tools, end on result."""
        pending = ""
        speaking = False
        sys.stdout.write("🤖 ")
        sys.stdout.flush()

        async for ev in events:
            if isinstance(ev, AssistantTextDelta):
                sys.stdout.write(ev.text)
                sys.stdout.flush()
                pending += ev.text
                pending, ready = _drain_sentences(pending)
                for sentence in ready:
                    speaking = self._begin_speaking(speaking)
                    await self._speak(sentence)
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
                if pending.strip():
                    speaking = self._begin_speaking(speaking)
                    await self._speak(pending)
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

        if speaking:
            self.mic.set_muted(False)
            self.vad.reset()

    def _begin_speaking(self, speaking: bool) -> bool:
        """Mark the start of agent speech, muting the mic in half-duplex mode.

        Half-duplex (the default) mutes the mic so the agent's own voice can't
        retrigger the VAD. Returns the updated `speaking` flag.
        """
        if speaking:
            return True
        if self.cfg.half_duplex:
            self.mic.set_muted(True)
        else:
            # TODO: full-duplex barge-in — keep the mic live during playback and
            # stop the Speaker when fresh speech is detected. Needs acoustic echo
            # cancellation; not implemented, so we leave the mic untouched here.
            pass
        return True

    async def _speak(self, text: str) -> None:
        await self.speaker.play(self.tts.synthesize(text))

    async def _handle_permission(self, ev: PermissionRequest) -> None:
        """Spoken approve/deny gate for one paused tool call.

        Speak a short summary, hand the mic over for one utterance, match it
        against the intent grammar. Unclear -> re-ask once -> deny. The CLI
        blocks until respond_permission lands, so the turn simply resumes (or
        skips the tool) afterward.
        """
        detail = _permission_detail(ev)
        print(f"\n   [permission] {ev.tool_name}: {detail}", flush=True)
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

    async def _listen_once(self, timeout: float) -> str:
        """Capture and transcribe a single utterance (for the permission gate).

        Opens a temporary segmenter over the shared mic frame stream — safe
        because the main listen loop in run() is parked awaiting _consume_turn
        and never competes for frames mid-turn. Timeout or stream end -> "".
        """
        agen = segment_utterances(self.mic.frames(), self.vad, self.cfg)
        try:
            utterance = await asyncio.wait_for(anext(agen), timeout)
        except (StopAsyncIteration, asyncio.TimeoutError):
            return ""
        finally:
            await agen.aclose()
        return await self.stt.transcribe(utterance, self.mic.sample_rate)


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

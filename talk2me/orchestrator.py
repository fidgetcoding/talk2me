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

import re
import sys

from .audio import Mic, Speaker
from .config import Config
from .events import (
    AssistantTextDelta,
    BackendError,
    SessionReady,
    ToolActivity,
    TurnComplete,
)
from .protocols import STT, TTS, VAD, AgentBackend
from .segment import segment_utterances

_SENTENCE = re.compile(r"(.+?[.!?]+)(\s+|$)", re.DOTALL)

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
                await self.backend.send(text)
                await self._consume_turn(events)
                if self._fatal:
                    print("\nbackend gone — shutting down.", flush=True)
                    break
                print("\n🎧 listening…", flush=True)
        finally:
            self.mic.stop()
            await self.backend.close()

    async def _consume_turn(self, events) -> None:
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

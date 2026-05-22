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

    async def run(self) -> None:
        await self.backend.start()
        self.mic.start()
        events = self.backend.events()
        print("talk2me ready — start talking. Ctrl-C to quit.\n", flush=True)
        try:
            async for utterance in segment_utterances(
                self.mic.frames(), self.vad, self.cfg
            ):
                text = await self.stt.transcribe(utterance, self.mic.sample_rate)
                if not text:
                    continue
                print(f"\n🗣  you: {text}", flush=True)
                await self.backend.send(text)
                await self._consume_turn(events)
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
                    if not speaking:
                        self.mic.set_muted(True)
                        speaking = True
                    await self._speak(sentence)
            elif isinstance(ev, ToolActivity):
                print(f"\n   [tool] {ev.name}", flush=True)
            elif isinstance(ev, TurnComplete):
                if pending.strip():
                    if not speaking:
                        self.mic.set_muted(True)
                        speaking = True
                    await self._speak(pending)
                print(flush=True)
                break
            elif isinstance(ev, SessionReady):
                continue
            elif isinstance(ev, BackendError):
                print(f"\n[backend error] {ev.message}", flush=True)
                break

        if speaking:
            self.mic.set_muted(False)
            self.vad.reset()

    async def _speak(self, text: str) -> None:
        await self.speaker.play(self.tts.synthesize(text))


def _drain_sentences(buf: str) -> tuple[str, list[str]]:
    """Pull complete sentences out of `buf`; return (remainder, [sentences])."""
    out: list[str] = []
    last = 0
    for m in _SENTENCE.finditer(buf):
        out.append(m.group(1).strip())
        last = m.end()
    return buf[last:], out

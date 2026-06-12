"""macOS `say` TTS. Synthesizes to a temp WAV, then yields PCM blocks.

`say` is free, offline, and always present on macOS. We render the whole chunk
to a temp file (LEI16 @ 16 kHz mono), then stream it back in small blocks so the
Speaker can stop mid-utterance for barge-in. Swap in ElevenLabs/Kitten later by
satisfying the same TTS Protocol — true streaming synthesis, no file hop.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import wave
from collections.abc import AsyncIterator

import numpy as np

_BLOCK = 2048  # samples per yielded block (~128 ms @ 16 kHz)

# Latency note (perf P0): `say` is batch — it renders the whole utterance to a
# temp WAV before we read a single sample, costing ~300–700ms of dead air per
# sentence. Streaming `say -o -` / `-o /dev/stdout` was evaluated and rejected:
# `say` writes a *seekable* WAV and cannot back-patch the RIFF header on a pipe,
# so a piped run emits only a ~32-byte stub header instead of PCM (verified
# headless 2026-05-22). The temp-file path is the only one that yields correct
# audio, so we keep it. The streaming win belongs to a true streaming TTS engine
# (ElevenLabs / a frame-yielding neural model), not to `say`.


class SayTTS:
    sample_rate: int = 16000

    def __init__(self, *, voice: str | None = None, rate_wpm: int | None = None) -> None:
        self._voice = voice
        self._rate_wpm = rate_wpm

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        text = text.strip()
        if not text:
            return
        # _render owns the temp file's whole lifetime: it cleans up on any
        # synthesis failure and returns None instead of raising, so a bad `say`
        # invocation degrades to silence for this sentence rather than tearing
        # down the conversation loop. A returned path always has a live file.
        path = await asyncio.to_thread(self._render, text)
        if path is None:
            return
        try:
            # Read-back in a worker thread (file I/O must not block the event
            # loop that drives the mic). _load_pcm returns None on a malformed
            # WAV instead of raising — synthesis failure degrades to silence,
            # never tears down the conversation loop.
            pcm = await asyncio.to_thread(self._load_pcm, path)
        finally:
            self._unlink(path)
        if pcm is None:
            return
        for i in range(0, pcm.shape[0], _BLOCK):
            yield pcm[i : i + _BLOCK]

    def _render(self, text: str) -> str | None:
        """Render `text` to a temp WAV and return its path, or None on failure.

        The temp file is created and owned here: if `say` fails (bad voice,
        unwritable tempdir, a payload it chokes on) or the read-back validation
        trips, we unlink the file before returning so nothing leaks in $TMPDIR.
        Returning None (rather than raising) lets `synthesize` skip the sentence
        and keep the loop alive — synthesis failure must not crash the broker.
        """
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="t2m_say_")
        os.close(fd)
        argv = ["say", "-o", path, "--data-format=LEI16@16000"]
        if self._voice:
            argv += ["-v", self._voice]
        if self._rate_wpm:
            argv += ["-r", str(self._rate_wpm)]
        argv += ["--", text]
        try:
            # Blocking subprocess in a worker thread (we're already off-loop).
            subprocess.run(argv, check=True, capture_output=True)
        except (subprocess.CalledProcessError, OSError):
            # `say` exited non-zero or could not be spawned: drop the temp file
            # and degrade to silence. Re-raising here would crash the orchestrator.
            self._unlink(path)
            return None
        except BaseException:
            # Thread cancellation / KeyboardInterrupt between mkstemp and return:
            # still clean up the file, but let the control-flow signal propagate.
            self._unlink(path)
            raise
        return path

    @staticmethod
    def _unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _load_pcm(path: str) -> np.ndarray | None:
        """Read the rendered WAV back as float32 mono, or None if it's malformed.

        `say` exiting 0 *should* guarantee a valid file, but a full disk or a
        mid-write kill can leave a truncated/garbage WAV; wave.open raises on
        those. Returning None lets synthesize() skip the sentence — a corrupt
        render must not crash the broker (blocking; runs off-loop).
        """
        try:
            with wave.open(path, "rb") as wf:
                raw = wf.readframes(wf.getnframes())
        except (wave.Error, EOFError, OSError):
            return None
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0

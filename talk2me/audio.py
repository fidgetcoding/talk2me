"""Audio I/O via sounddevice. Mic capture (async frame queue) + cancellable playback.

Playback is chunk-fed so barge-in can stop it mid-utterance: call Speaker.stop()
and the current synthesize→play pipeline is abandoned within one block (~tens of ms).
"""

from __future__ import annotations

import asyncio

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - import-time hardware/portaudio issues
    sd = None


class Mic:
    """Streams float32 mono frames of `frame_samples` length onto an asyncio queue."""

    def __init__(self, sample_rate: int, frame_samples: int) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self._loop = asyncio.get_event_loop()
        self._stream: sd.InputStream | None = None
        self._muted = False

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if self._muted:
            return
        # indata: (frames, 1) float32. Copy — sounddevice reuses the buffer.
        frame = indata[:, 0].copy()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, frame)

    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.frame_samples,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    async def frames(self):
        while True:
            yield await self._queue.get()


class Speaker:
    """Plays float32 mono PCM blocks. stop() abandons the current playback."""

    def __init__(self, sample_rate: int) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self._cancel = asyncio.Event()

    def stop(self) -> None:
        self._cancel.set()

    async def play(self, blocks) -> bool:
        """Play an async-iterable of PCM blocks. Returns False if interrupted."""
        self._cancel.clear()
        stream = sd.OutputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32"
        )
        stream.start()
        try:
            async for block in blocks:
                if self._cancel.is_set():
                    return False
                # write() blocks; hand it to a thread so the event loop (and the
                # mic VAD that drives barge-in) keeps running.
                await asyncio.to_thread(stream.write, block.astype(np.float32))
            return not self._cancel.is_set()
        finally:
            stream.stop()
            stream.close()

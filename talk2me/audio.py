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

# Cap on the mic frame backlog. At 16 kHz / 30 ms frames that's ~33 frames/sec,
# so this holds ~3 s of audio — enough headroom for normal scheduling jitter,
# small enough that a stalled consumer (slow STT, long backend turn) can't grow
# memory unboundedly or feed seconds-stale audio into the next segmentation pass.
_MIC_QUEUE_MAXSIZE = 100


class Mic:
    """Streams float32 mono frames of `frame_samples` length onto an asyncio queue.

    The queue is bounded (`_MIC_QUEUE_MAXSIZE`) with a drop-oldest policy: when the
    consumer falls behind, the oldest frame is discarded so the segmenter always
    sees recent audio rather than chewing through a stale backlog.
    """

    def __init__(self, sample_rate: int, frame_samples: int) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(
            maxsize=_MIC_QUEUE_MAXSIZE
        )
        # Bound at start() via get_running_loop(); the audio callback fires on a
        # PortAudio thread and needs a reference to the loop that actually ticks.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: sd.InputStream | None = None
        self._muted = False
        # Count of PortAudio-reported overflow events (dropped input frames) plus
        # our own drop-oldest evictions. Surfaced so callers can see real-time loss.
        self.dropped_frames = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if self._muted:
            return
        if status:
            # PortAudio sets `status` on input overflow — frames were dropped at
            # the hardware/driver layer. Count it instead of silently ignoring.
            self.dropped_frames += 1
        # indata: (frames, 1) float32. Copy — sounddevice reuses the buffer.
        frame = indata[:, 0].copy()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._enqueue, frame)

    def _enqueue(self, frame: np.ndarray) -> None:
        """Push a frame, dropping the oldest on overflow. Runs on the event loop."""
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop-oldest: evict the stalest frame so the consumer sees recent audio.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - drained by consumer
                pass
            else:
                self.dropped_frames += 1
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:  # pragma: no cover - racing consumer refilled
                self.dropped_frames += 1

    def start(self) -> None:
        # Bind the loop here (always called from the running loop in run()), not in
        # __init__ — get_event_loop() is deprecated and a Mic built off-loop would
        # otherwise capture a loop that never ticks.
        self._loop = asyncio.get_running_loop()
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
    """Plays float32 mono PCM blocks. stop() abandons the current playback.

    The underlying PortAudio OutputStream is opened lazily on the first play() and
    reused across calls — opening one per sentence costs ~30-150 ms of startup
    latency and produces an audible click/gap at every sentence boundary. close()
    tears it down; stop() also tears it down (it ends a turn and also catches any
    error mid-write), so the device handle never leaks across a session.
    """

    def __init__(self, sample_rate: int) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self._cancel = asyncio.Event()
        self._stream: sd.OutputStream | None = None

    def _ensure_stream(self) -> sd.OutputStream:
        """Lazily open the shared OutputStream, reusing it across play() calls."""
        if self._stream is None:
            stream = sd.OutputStream(
                samplerate=self.sample_rate, channels=1, dtype="float32"
            )
            try:
                stream.start()
            except BaseException:
                # start() can fail (device busy, sample-rate unsupported) after the
                # stream object exists — close it so the device handle can't leak.
                stream.close()
                raise
            self._stream = stream
        return self._stream

    def close(self) -> None:
        """Tear down the shared OutputStream. Safe to call when none is open."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def stop(self) -> None:
        self._cancel.set()
        # Ending a turn (or aborting on error) — release the device so the next
        # turn reopens cleanly and a long idle gap doesn't hold the handle.
        self.close()

    async def play(self, blocks) -> bool:
        """Play an async-iterable of PCM blocks. Returns False if interrupted."""
        self._cancel.clear()
        try:
            stream = self._ensure_stream()
            async for block in blocks:
                if self._cancel.is_set():
                    return False
                # write() blocks; hand it to a thread so the event loop (and the
                # mic VAD that drives barge-in) keeps running.
                await asyncio.to_thread(stream.write, block.astype(np.float32))
            return not self._cancel.is_set()
        except BaseException:
            # On any error (or cancellation) tear the stream down so a half-broken
            # handle isn't reused on the next play().
            self.close()
            raise

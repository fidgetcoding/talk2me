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


def list_devices() -> list[dict]:
    """All PortAudio devices (raw dicts). Empty if portaudio is unavailable."""
    if sd is None:
        return []
    return list(sd.query_devices())


def devices_of_kind(kind: str) -> list[tuple[int, str]]:
    """(index, name) for every device that can do `kind` ('input' | 'output')."""
    if sd is None:
        return []
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    return [
        (i, d["name"]) for i, d in enumerate(sd.query_devices()) if d.get(key, 0) > 0
    ]


def resolve_device(spec: str | int | None, kind: str) -> int | None:
    """Map a device spec to a PortAudio index.

    None -> None (system default). An int (or all-digit string) -> that index.
    Any other string -> the first `kind` device whose name contains it
    (case-insensitive). Raises ValueError if a name spec matches nothing — better
    a clean startup error than silently capturing the wrong device.
    """
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    s = spec.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    needle = s.lower()
    for i, name in devices_of_kind(kind):
        if needle in name.lower():
            return i
    raise ValueError(f"no {kind} device matching {spec!r}")


# Names that mean the audio comes out of open-air speakers — where the mic can
# hear the TTS and full-duplex barge-in would argue with its own echo.
_SPEAKER_NAME_TOKENS = ("speaker", "built-in output")


def _name_looks_like_speakers(name: str) -> bool:
    n = name.lower()
    return any(tok in n for tok in _SPEAKER_NAME_TOKENS)


def output_is_speakers(device: int | None) -> bool:
    """Best-effort: is the (resolved or system-default) output open-air speakers?

    True only on a confident speaker match ("MacBook Pro Speakers", anything
    named *speaker*). Bluetooth/USB/jack devices have arbitrary names
    ("Megapods", "External Headphones") and are treated as headphones — the
    failure mode there is merely no barge-in downgrade, while a missed speaker
    means self-echo chaos, so the heuristic errs toward the speaker label only.
    (A Bluetooth *speaker* slips through; pass no --barge-in there.)
    """
    if sd is None:
        return False
    try:
        idx = device
        if idx is None:
            idx = sd.default.device[1]
        if idx is None or int(idx) < 0:
            return False
        name = sd.query_devices(int(idx))["name"]
    except Exception:
        return False
    return _name_looks_like_speakers(str(name))


def format_device_table() -> str:
    """Human-readable input/output listing for `--list-devices`."""
    if sd is None:
        return "sounddevice/portaudio unavailable — no devices to list."
    try:
        default_in, default_out = sd.default.device
    except Exception:  # pragma: no cover - portaudio without a default device
        default_in = default_out = None
    lines = ["INPUT devices (mic):"]
    for i, name in devices_of_kind("input"):
        lines.append(f"{'  *' if i == default_in else '   '} [{i}] {name}")
    lines.append("OUTPUT devices (playback):")
    for i, name in devices_of_kind("output"):
        lines.append(f"{'  *' if i == default_out else '   '} [{i}] {name}")
    lines.append("(* = system default. Pass an index or a name substring.)")
    return "\n".join(lines)


class Mic:
    """Streams float32 mono frames of `frame_samples` length onto an asyncio queue.

    The queue is bounded (`_MIC_QUEUE_MAXSIZE`) with a drop-oldest policy: when the
    consumer falls behind, the oldest frame is discarded so the segmenter always
    sees recent audio rather than chewing through a stale backlog.
    """

    def __init__(
        self, sample_rate: int, frame_samples: int, device: int | None = None
    ) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.device = device  # PortAudio index, or None for the system default
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

    def _open_stream(self) -> None:
        """Open + start an InputStream on the current device. Shared by start()
        and switch_device() so the two can't drift on stream config."""
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.frame_samples,
            device=self.device,
            callback=self._callback,
        )
        self._stream.start()

    def start(self) -> None:
        # Bind the loop here (always called from the running loop in run()), not in
        # __init__ — get_event_loop() is deprecated and a Mic built off-loop would
        # otherwise capture a loop that never ticks.
        self._loop = asyncio.get_running_loop()
        self._open_stream()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def switch_device(self, device: int | None) -> None:
        """Repoint the mic at `device`, reopening the stream if it's running.

        Mute state and the bound event loop are preserved, so cycling capture
        devices live (e.g. by hotkey) drops at most a frame or two rather than
        restarting the loop. A no-op-safe close precedes the reopen.
        """
        self.device = device
        if self._stream is not None:
            # Detach BEFORE closing/reopening: if _open_stream raises (bad index,
            # device unplugged mid-switch), self._stream must not keep pointing
            # at the already-closed old stream — a later stop() would double-close
            # it and raise during shutdown.
            stream, self._stream = self._stream, None
            stream.stop()
            stream.close()
            self._open_stream()

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

    def __init__(
        self, sample_rate: int, device: int | None = None, echo_ref=None
    ) -> None:
        if sd is None:
            raise RuntimeError("sounddevice/portaudio unavailable")
        self.sample_rate = sample_rate
        self.device = device  # PortAudio index, or None for the system default
        # Optional EchoRef: every block written to the output stream is also
        # recorded here, giving the echo gate its playback reference (what
        # the mic is about to hear coming off open-air speakers).
        self.echo_ref = echo_ref
        self._cancel = asyncio.Event()
        self._stream: sd.OutputStream | None = None
        # True while play() has a write in flight on a worker thread. stop()
        # must NOT close the stream in that window — see stop().
        self._playing = False

    def _ensure_stream(self) -> sd.OutputStream:
        """Lazily open the shared OutputStream, reusing it across play() calls."""
        if self._stream is None:
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=self.device,
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

    def switch_device(self, device: int | None) -> None:
        """Repoint playback at `device`. Closes the current stream; the next
        play() lazily reopens on the new device — so a live output switch takes
        effect on the next spoken sentence, with no click between turns."""
        self.device = device
        self.close()

    def stop(self) -> None:
        self._cancel.set()
        if self._playing and self._stream is not None:
            # A play() is mid-write on a worker thread (barge-in cutting live
            # playback). abort() drops the queued audio instantly, but the
            # close is deferred to play()'s cleanup — closing here races the
            # blocked write and crashes the loop (live-run bug: PortAudio
            # -9986 when barge-in stopped the speaker mid-sentence).
            try:
                self._stream.abort()
            except Exception:
                pass
            return
        # Idle stop (turn end, shutdown) — release the device so the next turn
        # reopens cleanly and a long idle gap doesn't hold the handle.
        self.close()

    async def play(self, blocks) -> bool:
        """Play an async-iterable of PCM blocks. Returns False if interrupted."""
        self._cancel.clear()
        self._playing = True
        try:
            stream = self._ensure_stream()
            async for block in blocks:
                if self._cancel.is_set():
                    return False
                # write() blocks; hand it to a thread so the event loop (and the
                # mic VAD that drives barge-in) keeps running.
                if self.echo_ref is not None:
                    self.echo_ref.add(block)
                try:
                    await asyncio.to_thread(stream.write, block.astype(np.float32))
                except sd.PortAudioError:
                    if self._cancel.is_set():
                        # stop() aborted the stream underneath this write — an
                        # expected cancellation, not a device failure.
                        return False
                    raise
            return not self._cancel.is_set()
        except BaseException:
            # On any error (or cancellation) tear the stream down so a half-broken
            # handle isn't reused on the next play().
            self.close()
            raise
        finally:
            self._playing = False
            if self._cancel.is_set():
                # Interrupted playback: the writer thread is out of the stream
                # now, so finish the close that stop() deferred.
                self.close()

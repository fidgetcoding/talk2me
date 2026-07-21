"""Phase 0 spike for v2.5 — does macOS voice processing (AUVoiceIO) clean the mic?

Three capture configs, same rendered `say` speech playing out the speakers:

  1. baseline   PortAudio mic          + PortAudio playback   (today's stack)
  2. vpio-pa    VPIO AVAudioEngine tap + PortAudio playback   → answers (a)
  3. vpio-node  VPIO AVAudioEngine tap + engine playerNode    → answers (b)

Tahoe quirk: enabling voice processing flips the input node to a 9-channel
deinterleaved format. Which of those carries the AEC'd voice is unknown, so
the tap keeps EVERY channel and the report shows per-channel echo-above-floor
— the processed channel is the quiet one, the loopback/reference channels are
the loud ones. Capture WAVs land in /tmp/spike_vpio_out/ for listening and
later STT checks.

Usage:  .venv/bin/python scripts/spike_vpio.py [--voice Ava] [--runs 1]
"""

from __future__ import annotations

import argparse
import math
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

SPEECH = (
    "Acoustic echo cancellation test. The engine subtracts its own playback "
    "from the microphone at the driver level. Counting: one, two, three, "
    "four, five, six, seven, eight, nine, ten. The quick brown fox jumps "
    "over the lazy dog while the canceller keeps listening."
)

RATE = 16000
OUT_DIR = Path(tempfile.gettempdir()) / "spike_vpio_out"


def render_speech(voice: str | None) -> tuple[str, np.ndarray]:
    fd = tempfile.NamedTemporaryFile(suffix=".wav", prefix="spike_vpio_", delete=False)
    fd.close()
    argv = ["say", "-o", fd.name, "--data-format=LEI16@16000"]
    if voice:
        argv += ["-v", voice]
    argv += ["--", SPEECH]
    subprocess.run(argv, check=True, capture_output=True)
    with wave.open(fd.name, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    return fd.name, pcm.astype(np.float32) / 32768.0


def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x))))


def dbfs(v: float) -> float:
    return 20.0 * math.log10(max(v, 1e-9))


def save_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


class PortAudioCapture:
    """Config-1 mic: plain sounddevice InputStream, float32 mono @16k."""

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self.sample_rate = RATE

    def start(self) -> None:
        def cb(indata, frames, time_info, status) -> None:  # noqa: ANN001
            self._chunks.append(indata[:, 0].copy())

        self._stream = sd.InputStream(
            samplerate=RATE, channels=1, dtype="float32", callback=cb
        )
        self._stream.start()

    def stop(self) -> list[np.ndarray]:
        assert self._stream is not None
        self._stream.stop()
        self._stream.close()
        mono = (
            np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        )
        return [mono]


class VPIOCapture:
    """Configs 2/3: AVAudioEngine input tap, voice processing on, ALL channels kept."""

    def __init__(self) -> None:
        from AVFoundation import AVAudioEngine

        self.engine = AVAudioEngine.alloc().init()
        self._chunks: list[list[np.ndarray]] = []
        self._lock = threading.Lock()
        self.sample_rate = 0.0
        self.channels = 0
        self.tap_format = ""

    def start(self) -> None:
        inp = self.engine.inputNode()
        ok, err = inp.setVoiceProcessingEnabled_error_(True, None)
        if not ok:
            raise RuntimeError(f"setVoiceProcessingEnabled failed: {err}")

        fmt = inp.outputFormatForBus_(0)
        self.sample_rate = float(fmt.sampleRate())
        self.channels = int(fmt.channelCount())
        self.tap_format = (
            f"{fmt.sampleRate():.0f}Hz {fmt.channelCount()}ch "
            f"{'deint' if not fmt.isInterleaved() else 'INTERLEAVED'}"
        )
        nch = self.channels
        for _ in range(nch):
            self._chunks.append([])

        def tap(buf, when) -> None:  # noqa: ANN001
            n = int(buf.frameLength())
            if n == 0:
                return
            ch = buf.floatChannelData()
            if ch is None:
                return
            with self._lock:
                for c in range(nch):
                    self._chunks[c].append(
                        np.array(ch[c].as_tuple(n), dtype=np.float32)
                    )

        inp.installTapOnBus_bufferSize_format_block_(0, 4096, fmt, tap)
        self.engine.prepare()
        ok, err = self.engine.startAndReturnError_(None)
        if not ok:
            raise RuntimeError(f"engine start failed: {err}")

    def stop(self) -> list[np.ndarray]:
        self.engine.inputNode().removeTapOnBus_(0)
        self.engine.stop()
        with self._lock:
            return [
                np.concatenate(c) if c else np.zeros(0, np.float32)
                for c in self._chunks
            ]


def play_portaudio(pcm: np.ndarray) -> None:
    sd.play(pcm, RATE, blocking=True)


def play_via_engine(capture: VPIOCapture, wav_path: str) -> None:
    """Config 3: schedule the WAV on a playerNode attached to the VPIO engine."""
    from AVFoundation import AVAudioFile, AVAudioPlayerNode
    from Foundation import NSURL

    url = NSURL.fileURLWithPath_(wav_path)
    afile, err = AVAudioFile.alloc().initForReading_error_(url, None)
    if afile is None:
        raise RuntimeError(f"AVAudioFile open failed: {err}")
    player = AVAudioPlayerNode.alloc().init()
    capture.engine.attachNode_(player)
    capture.engine.connect_to_format_(
        player, capture.engine.mainMixerNode(), afile.processingFormat()
    )
    player.scheduleFile_atTime_completionHandler_(afile, None, None)
    player.play()
    dur = float(afile.length()) / float(afile.processingFormat().sampleRate())
    # completion handlers fire at schedule/consume time, not through-the-speaker
    # time — wall-clock the file duration instead.
    time.sleep(dur + 0.3)
    player.stop()


def run_config(label: str, capture, play, floor_s: float = 1.5, tail_s: float = 0.5):
    capture.start()
    time.sleep(floor_s)
    t0 = time.monotonic()
    play()
    play_dur = time.monotonic() - t0
    time.sleep(tail_s)
    channels = capture.stop()

    sr = int(capture.sample_rate or RATE)
    per_ch = []
    for audio in channels:
        floor = audio[int(0.3 * sr) : int((floor_s - 0.2) * sr)]
        echo = audio[int((floor_s + 0.3) * sr) : int((floor_s + play_dur - 0.3) * sr)]
        per_ch.append(
            {
                "floor_dbfs": round(dbfs(rms(floor)), 1),
                "echo_dbfs": round(dbfs(rms(echo)), 1),
                "echo_above_floor_db": round(dbfs(rms(echo)) - dbfs(rms(floor)), 1),
            }
        )

    OUT_DIR.mkdir(exist_ok=True)
    for c, audio in enumerate(channels):
        if audio.size:
            save_wav(OUT_DIR / f"{label}-ch{c}.wav", audio, sr)

    out = {
        "config": label,
        "sample_rate": sr,
        "play_dur_s": round(play_dur, 1),
        "captured_s": round(channels[0].size / sr, 2) if sr else 0,
        "channels": per_ch,
    }
    if isinstance(capture, VPIOCapture):
        out["tap_format"] = capture.tap_format
    return out


def report(r: dict) -> None:
    head = f"{r['config']}  sr={r['sample_rate']}  played={r['play_dur_s']}s  captured={r['captured_s']}s"
    if "tap_format" in r:
        head += f"  [{r['tap_format']}]"
    print(head)
    for c, ch in enumerate(r["channels"]):
        print(
            f"   ch{c}: floor {ch['floor_dbfs']:6.1f}  echo {ch['echo_dbfs']:6.1f}"
            f"  above-floor {ch['echo_above_floor_db']:+6.1f} dB"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--skip-baseline", action="store_true")
    args = ap.parse_args()

    wav_path, pcm = render_speech(args.voice)
    print(f"speech rendered: {pcm.size / RATE:.1f}s")
    print(f"output device: {sd.query_devices(sd.default.device[1])['name']}")
    print(f"input device : {sd.query_devices(sd.default.device[0])['name']}")
    print(f"captures -> {OUT_DIR}")

    for run in range(args.runs):
        print(f"\n=== run {run + 1}/{args.runs} ===")
        if not args.skip_baseline:
            report(run_config("1-baseline", PortAudioCapture(), lambda: play_portaudio(pcm)))
            time.sleep(1.0)
        cap2 = VPIOCapture()
        report(run_config("2-vpio+portaudio", cap2, lambda: play_portaudio(pcm)))
        time.sleep(1.0)
        cap3 = VPIOCapture()
        report(run_config("3-vpio+playerNode", cap3, lambda: play_via_engine(cap3, wav_path)))

    Path(wav_path).unlink(missing_ok=True)
    print("\nnext: listen to the ch WAVs — the AEC'd voice channel is the one where")
    print("the speech is gone; loopback/reference channels are the loud ones.")


if __name__ == "__main__":
    main()

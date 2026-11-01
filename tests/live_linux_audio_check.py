"""The real voice loop through real PortAudio, on a Linux box with fake sound cards.

This is the live mic path: `python -m talk2me` as a real process, the real Mic and
Speaker classes, real PortAudio, a real PulseAudio server, real base.en, the real
Silero speech gate, the real permission wire. Two null sinks stand in for hardware:
a clip played into "t2m_room" comes out of the fake mic, and whatever talk2me says
lands in "t2m_speaker", where this records it. Only the agent is a stand-in
(fake_claude_cli.py), and the voice is espeak-ng, so nobody's talking.

What it doesn't prove: a real microphone, a real room, real echo, real drivers.

It refuses to run anywhere but Linux with the virtual devices below set up, because
it plays sound. Never point it at a machine with real audio.

Setup and run are in .github/workflows/ci.yml (job linux-live).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

RESULTS: list[bool] = []
REQUIRED_SOURCE = "t2m_room.monitor"
REQUIRED_SINK = "t2m_speaker"
FAKE = Path(__file__).with_name("fake_claude_cli.py")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    RESULTS.append(bool(ok))


def _refuse_real_audio() -> None:
    if sys.platform != "linux" or os.environ.get("T2M_VIRTUAL_AUDIO") != "1":
        raise SystemExit("refusing: this plays sound, and only runs on Linux with T2M_VIRTUAL_AUDIO=1")
    source = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, check=True).stdout.strip()
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
    if source != REQUIRED_SOURCE or sink != REQUIRED_SINK:
        raise SystemExit(f"refusing: default devices are {source!r} and {sink!r}, not the virtual pair")


def _clip(folder: Path, name: str, text: str) -> Path:
    # Slow and with a lead-in of silence: espeak is hard enough on Whisper already.
    path = folder / f"{name}.wav"
    subprocess.run(["espeak-ng", "-s", "135", "-w", str(path), f"  {text}"], check=True)
    return path


def _say_into_room(path: Path) -> None:
    subprocess.run(["paplay", "--device=t2m_room", str(path)], check=True)


class Session:
    """t2m as a real child process, its stdout tailed so the test can wait on it."""

    def __init__(self, folder: Path, args: list[str], script: list[dict]) -> None:
        shim = folder / "bin"
        shim.mkdir()
        claude = shim / "claude"
        claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
        claude.chmod(0o755)
        self.log_path = folder / "agent.jsonl"
        env = {k: v for k, v in os.environ.items() if k != "TALK2ME_SAVE_DIR"}
        env.update(
            PATH=f"{shim}{os.pathsep}{env['PATH']}",
            FAKE_CLAUDE_SCRIPT=json.dumps(script),
            FAKE_CLAUDE_LOG=str(self.log_path),
            TALK2ME_CONFIG=str(folder / "no-config.json"),
            TALK2ME_STATE=str(folder / "state.json"),
            PYTHONUNBUFFERED="1",
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "talk2me", "--cwd", str(folder), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        self.lines: list[str] = []
        threading.Thread(target=self._tail, daemon=True).start()

    def _tail(self) -> None:
        for line in self.proc.stdout:
            print("   t2m|", line.rstrip(), flush=True)
            self.lines.append(line)

    def wait_for(self, pattern: str, timeout: float, *, start: int = 0) -> int | None:
        """Index of the first line from `start` on that matches, or None on timeout."""
        deadline = time.monotonic() + timeout
        seen = start
        while time.monotonic() < deadline:
            while seen < len(self.lines):
                if re.search(pattern, self.lines[seen]):
                    return seen
                seen += 1
            if self.proc.poll() is not None:
                return None
            time.sleep(0.05)
        return None

    def agent_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def stop(self) -> int | None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
                return None
        return self.proc.returncode


def _rms_seconds(path: Path) -> float:
    """Seconds of non-silence in a recording."""
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    frame = rate // 50
    if len(pcm) < frame:
        return 0.0
    frames = pcm[: len(pcm) // frame * frame].reshape(-1, frame)
    return float(np.sum(np.sqrt(np.mean(frames ** 2, axis=1)) > 0.003)) / 50


def _record_speaker(path: Path) -> subprocess.Popen:
    return subprocess.Popen([
        "parecord", "--device=t2m_speaker.monitor", "--file-format=wav",
        "--format=s16le", "--rate=16000", "--channels=1", str(path),
    ])


def test_full_turn(work: Path) -> None:
    print("--- a turn, the spoken gate, and a barge-in, half and full duplex", flush=True)
    ask = _clip(work, "ask", "Please run the tests for me.")
    approve = _clip(work, "approve", "Approve.")
    interrupt = _clip(work, "interrupt", "Stop talking and listen to me now, please.")
    recording = work / "speaker.wav"
    recorder = _record_speaker(recording)
    folder = work / "turn"
    folder.mkdir()
    long_reply = " ".join(f"This is sentence number {n} of a long reply that nobody wants to hear." for n in range(1, 12))
    session = Session(folder, ["--tts", "espeak", "--debug"], [
        {"ask": "npm test", "say": "The tests pass."},
        {"say": long_reply, "hold": 30},
        {"say": "Okay, listening."},
    ])
    try:
        ready = session.wait_for(r"listening", 240)
        check("reaches listening with real devices", ready is not None)
        if ready is None:
            return
        card = "".join(session.lines)
        check("gated by default", "gated (spoken approvals)" in card)

        _say_into_room(ask)
        heard = session.wait_for(r"you: ", 60)
        check("mic -> speech gate -> whisper: heard the sentence", heard is not None)
        users = [e["text"] for e in session.agent_log() if e.get("event") == "user"]
        words = " ".join(users).lower()
        check("the agent got recognizable words", "test" in words, str(users))

        asked = session.wait_for(r"\[permission\] Bash", 30)
        check("gate fired on the real wire", asked is not None)
        time.sleep(4)  # let the spoken question finish
        _say_into_room(approve)
        verdict = session.wait_for(r"APPROVED|DENIED", 60)
        behaviors = [e["behavior"] for e in session.agent_log() if e.get("event") == "permission"]
        check("spoken approve reached the agent", behaviors == ["allow"], str(behaviors))
        check("said so on screen", verdict is not None and "APPROVED" in session.lines[verdict])

        mark = len(session.lines)
        listening = session.wait_for(r"listening", 60, start=mark)
        check("ears reopened after the reply", listening is not None)

        _say_into_room(ask)
        session.wait_for(r"you: ", 60, start=mark)
        time.sleep(7)  # well into the long reply
        _say_into_room(interrupt)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and not any(e.get("event") == "interrupt" for e in session.agent_log()):
            time.sleep(0.2)
        check("talking over it interrupted the agent", any(e.get("event") == "interrupt" for e in session.agent_log()))
    finally:
        code = session.stop()
        recorder.terminate()
        recorder.wait(5)
    check("Ctrl-C exits cleanly", code == 0, f"exit {code}")
    spoken = _rms_seconds(recording)
    check("its voice really reached the speaker device", spoken > 3, f"{spoken:.1f}s of sound")


def test_pause_and_wake(work: Path) -> None:
    print("--- pause and wake, half duplex", flush=True)
    # Not "pause listening": espeak says that so badly Whisper hears "pause this onion".
    pause = _clip(work, "pause", "Go to sleep.")
    wake = _clip(work, "wake", "Wake up.")
    ignored = _clip(work, "ignored", "This sentence should go nowhere at all.")
    folder = work / "pause"
    folder.mkdir()
    session = Session(folder, ["--tts", "espeak", "--no-barge-in"], [{"say": "Hello again."}])
    try:
        check("second launch reaches listening", session.wait_for(r"listening", 240) is not None)
        _say_into_room(pause)
        check("spoken pause lands", session.wait_for(r"paused", 40) is not None)
        time.sleep(3)
        _say_into_room(ignored)
        time.sleep(6)
        users = [e for e in session.agent_log() if e.get("event") == "user"]
        check("paused means nothing reaches the agent", users == [], str(users))
        _say_into_room(wake)
        check("spoken wake lands", session.wait_for(r"awake", 40) is not None)
    finally:
        code = session.stop()
    check("clean exit again", code == 0, f"exit {code}")


def main() -> int:
    _refuse_real_audio()
    for tool in ("espeak-ng", "paplay", "parecord"):
        if not shutil.which(tool):
            raise SystemExit(f"missing {tool}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        test_full_turn(work)
        test_pause_and_wake(work)
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

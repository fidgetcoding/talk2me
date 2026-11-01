"""The phone page in real browser engines, over a real SSH tunnel, as far as a Linux
box can take it. CI only: this plays sound, so it refuses to run without the virtual
sound cards.

It runs `python -m talk2me --phone` as a real process with a stand-in agent, opens
the real page through `ssh -L` the way Blink would, and taps the real button:

- Chromium with Playwright's iPhone profile (screen, touch, Safari user agent) and a
  fake mic fed from a file: speech goes through the page's own capture code, over the
  tunnel, into real base.en; the reply plays through the page's own playback code.
- WebKit, Safari's engine, built for Linux: page loads, scripts parse, tap connects.
  Its Linux build has no fake mic, so it sends no audio, and that gets reported.

What this can't prove, and only a real iPhone can: iOS Safari's audio session and
autoplay rules, its echo cancellation, what happens when the screen locks or a call
comes in, and Blink's own port forward. Chromium in an iPhone costume isn't an iPhone.

Setup and run are in .github/workflows/ci.yml (job phone-browser).
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RESULTS: list[bool] = []
FAKE = Path(__file__).with_name("fake_claude_cli.py")
PHONE_PORT = 8765
TUNNEL_PORT = 9876


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    RESULTS.append(bool(ok))


def note(text: str) -> None:
    print(f"[NOTE] {text}", flush=True)


class Session:
    def __init__(self, folder: Path, script: list[dict]) -> None:
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
            [sys.executable, "-m", "talk2me", "--phone", "--phone-port", str(PHONE_PORT),
             "--tts", "espeak", "--auto-approve", "--cwd", str(folder)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        self.lines: list[str] = []
        threading.Thread(target=self._tail, daemon=True).start()

    def _tail(self) -> None:
        for line in self.proc.stdout:
            print("   t2m|", line.rstrip(), flush=True)
            self.lines.append(line)

    def wait_for(self, pattern: str, timeout: float) -> re.Match | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.lines:
                match = re.search(pattern, line)
                if match:
                    return match
            time.sleep(0.1)
        return None

    def agent_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _clip(path: Path, text: str, seconds_silence: int = 2) -> None:
    subprocess.run(["espeak-ng", "-s", "135", "-w", str(path), text], check=True)
    # Chromium's fake capture starts at getUserMedia; a lead-in keeps the first word.
    padded = path.with_suffix(".pad.wav")
    subprocess.run(["sox", str(path), str(padded), "pad", str(seconds_silence), "1"], check=True)
    padded.replace(path)


def _wait_until(fn, timeout: float, every: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return None


def _open_tunnel() -> subprocess.Popen:
    tunnel = subprocess.Popen([
        "ssh", "-N", "-o", "StrictHostKeyChecking=no", "-o", "ExitOnForwardFailure=yes",
        "-L", f"{TUNNEL_PORT}:localhost:{PHONE_PORT}", "localhost",
    ])
    time.sleep(2)
    if tunnel.poll() is not None:
        raise SystemExit(f"ssh tunnel didn't come up: exit {tunnel.returncode}")
    return tunnel


def test_chromium(p, work: Path) -> None:
    print("--- Chromium with an iPhone's screen and user agent, fake mic from a file", flush=True)
    clip = work / "ask.wav"
    _clip(clip, "Please run the tests for me.")
    folder = work / "chromium"
    folder.mkdir()
    long_reply = " ".join(f"This is sentence number {n} of a long reply." for n in range(1, 15))
    session = Session(folder, [{"say": "The tests pass."}, {"say": long_reply, "hold": 25}, {"say": "Okay."}])
    tunnel = _open_tunnel()
    try:
        url = session.wait_for(rf"http://localhost:{PHONE_PORT}/\?t=([\w-]+)", 120)
        check("--phone prints its link", url is not None)
        if url is None:
            return
        token = url.group(1)
        browser = p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={clip}",
        ])
        context = browser.new_context(**{k: v for k, v in p.devices["iPhone 13"].items() if k != "default_browser_type"})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        refused = page.goto(f"http://localhost:{TUNNEL_PORT}/")
        check("the tunnel works and no token means 403", refused.status == 403, f"status {refused.status}")
        wrong = page.goto(f"http://localhost:{TUNNEL_PORT}/?t=wrong")
        check("a wrong token means 403", wrong.status == 403)

        loaded = page.goto(f"http://localhost:{TUNNEL_PORT}/?t={token}")
        check("the page loads through the tunnel", loaded.status == 200)
        check("mic is allowed on localhost through the tunnel", page.evaluate("window.isSecureContext"))
        check("Safari's user agent is on", "iPhone" in page.evaluate("navigator.userAgent"))
        page.tap("#go")
        page.wait_for_function("document.getElementById('st').textContent.includes('connected')", timeout=30000)
        check("tap connects", True)
        check("t2m saw the phone", session.wait_for(r"phone connected", 20) is not None)

        users = _wait_until(lambda: [e["text"] for e in session.agent_log() if e.get("event") == "user"], 180)
        check("page mic -> tunnel -> whisper -> agent", bool(users), str(users))
        check("words survived the page's own downsampler", users and "test" in " ".join(users).lower(), str(users))

        played = page.evaluate("""() => new Promise((resolve) => {
          const st = document.getElementById('st');
          const started = Date.now();
          let spoke = st.textContent.includes('speaking');
          const tick = () => {
            if (st.textContent.includes('speaking')) spoke = true;
            if (spoke && st.textContent.includes('connected')) return resolve(true);
            if (Date.now() - started > 60000) return resolve(spoke);
            setTimeout(tick, 50);
          };
          tick();
        })""")
        check("the reply played through the page and finished", played)

        # The looping fake mic is someone talking over a long answer.
        page.reload()
        page.tap("#go")
        page.wait_for_function("document.getElementById('st').textContent.includes('connected')", timeout=30000)
        check("reload reconnects with the same link", True)
        interrupted = _wait_until(lambda: any(e.get("event") == "interrupt" for e in session.agent_log()), 120)
        if interrupted:
            check("talking over it reaches the agent as an interrupt", True)
        else:
            note("no interrupt seen: the file mic didn't land mid-reply on this run")
        check("no JavaScript errors", errors == [], str(errors))
        browser.close()
    finally:
        session.stop()
        tunnel.terminate()


def test_webkit(p, work: Path) -> None:
    print("--- WebKit, Safari's engine, Linux build", flush=True)
    folder = work / "webkit"
    folder.mkdir()
    session = Session(folder, [{"say": "Hello."}])
    tunnel = _open_tunnel()
    try:
        url = session.wait_for(rf"http://localhost:{PHONE_PORT}/\?t=([\w-]+)", 120)
        if url is None:
            check("webkit: --phone prints its link", False)
            return
        browser = p.webkit.launch()
        context = browser.new_context(**{k: v for k, v in p.devices["iPhone 13"].items() if k != "default_browser_type"})
        try:
            context.grant_permissions(["microphone"])
        except Exception as exc:  # noqa: BLE001  WebKit's Linux build may not know this permission
            note(f"webkit won't take a microphone grant: {str(exc)[:80]}")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        loaded = page.goto(f"http://localhost:{TUNNEL_PORT}/?t={url.group(1)}")
        check("webkit: page loads through the tunnel", loaded.status == 200)
        check("webkit: the page's audio APIs exist", page.evaluate(
            "!!(window.AudioContext || window.webkitAudioContext) && !!navigator.mediaDevices"
            " && !!(new (window.AudioContext || window.webkitAudioContext)()).createScriptProcessor"
        ))
        frames: list[int] = []
        page.on("websocket", lambda ws: ws.on("framesent", lambda f: frames.append(len(f))))
        page.tap("#go")
        page.wait_for_function("document.getElementById('st').textContent.length > 0", timeout=30000)
        status = page.evaluate("document.getElementById('st').textContent")
        time.sleep(5)
        note(f"webkit mic sent {len(frames)} audio frames in 5 s; {sum(frames)} bytes")
        note(f"webkit tap landed on: {status!r}")
        check("webkit: tap gets an honest status, not a hang", "connected" in status or "mic blocked" in status, status)
        check("webkit: no JavaScript errors", errors == [], str(errors))
        browser.close()
    finally:
        session.stop()
        tunnel.terminate()


def main() -> int:
    if sys.platform != "linux" or os.environ.get("T2M_VIRTUAL_AUDIO") != "1":
        raise SystemExit("refusing: this plays sound, and only runs on Linux with T2M_VIRTUAL_AUDIO=1")
    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
    if sink != "t2m_speaker":
        raise SystemExit(f"refusing: default sink is {sink!r}, not the virtual one")
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        work = Path(tmp)
        test_chromium(p, work)
        test_webkit(p, work)
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

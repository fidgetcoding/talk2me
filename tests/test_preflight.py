"""What a new user sees when something's missing or mistyped: one sentence, not a
traceback. Pure checks, and real runs of the CLI that stop before any audio opens.

Run:  python -m tests.test_preflight
"""

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from talk2me import __version__, factory, preflight

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


def _cli(args: list[str], *, path: str | None = None, cwd: str | None = None) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if k != "TALK2ME_SAVE_DIR"}
        env.update(TALK2ME_CONFIG=str(Path(tmp, "none.json")), TALK2ME_STATE=str(Path(tmp, "state.json")), PYTHONUTF8="1")
        if path is not None:
            env["PATH"] = path
        proc = subprocess.run(
            [sys.executable, "-m", "talk2me", *args], capture_output=True, text=True, encoding="utf-8",
            input="", env=env, cwd=cwd, timeout=120, check=False,
        )
        return proc.returncode, proc.stdout + proc.stderr


def _stub_path(folder: Path) -> str:
    """A PATH with a stand-in claude, so these runs can never reach a real account."""
    fake = Path(__file__).with_name("fake_claude_cli.py")
    if sys.platform == "win32":
        (folder / "claude.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n')
    else:
        (folder / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
        (folder / "claude").chmod(0o755)
    return f"{folder}{os.pathsep}{os.environ['PATH']}"


def test_checks() -> None:
    check("an agent nobody's heard of is still named", "not-a-real-agent" in (preflight.agent_problem("not-a-real-agent") or ""))
    check("a bad --cwd is caught", preflight.cwd_problem("/no/such/folder") is not None)
    check("no --cwd is fine", preflight.cwd_problem(None) is None)
    home = os.path.realpath(tempfile.gettempdir())
    check("sandbox refuses home", preflight.sandbox_project_problem(home, home) is not None)
    check("sandbox refuses a parent of home", preflight.sandbox_project_problem(os.path.dirname(home), home) is not None)
    check("sandbox accepts a project", preflight.sandbox_project_problem(os.path.join(home, "project"), home) is None)
    check("similar names aren't parents", preflight.sandbox_project_problem(home + "-other", home) is None)
    check("a typo'd whisper model is caught", "base.en" in (preflight.whisper_model_problem("bass.en") or ""))
    check("real whisper models pass", preflight.whisper_model_problem("small.en") is None)
    check("a Hugging Face repo passes", preflight.whisper_model_problem("someone/some-model") is None)
    model, lang, note = preflight.match_model_to_language("whisper", "base.en", "es")
    check(".en model moves to its twin for Spanish", model == "base" and lang == "es" and note)
    model, lang, note = preflight.match_model_to_language("whisper", "small.en", "auto")
    check(".en model moves for auto too", model == "small" and note)
    check("English changes nothing", preflight.match_model_to_language("whisper", "base.en", "en") == ("base.en", "en", None))
    check("gpu whisper is left alone", preflight.match_model_to_language("whisper-gpu", "base.en", "es")[1:] == ("es", None))
    check("parakeet falls back to English", preflight.match_model_to_language("parakeet", "base.en", "es")[1] == "en")


def test_engines_that_are_gone() -> None:
    check("saved engine that no longer exists", factory.resolve_stt("mlx-whisper")[0] == "whisper" and factory.resolve_stt("mlx-whisper")[1])
    real = factory.whisper_gpu_supported
    factory.whisper_gpu_supported = lambda: False
    try:
        check("parakeet off Apple Silicon falls back", factory.resolve_stt("parakeet")[0] == "whisper")
    finally:
        factory.whisper_gpu_supported = real


def test_real_cli() -> None:
    code, out = _cli(["--version"])
    check("--version answers", code == 0 and __version__ in out and __version__ != "unknown", out.strip())
    code, out = _cli(["--help"])
    check("--help talks to people", code == 0 and "start here:" in out and "Maps argv" not in out)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        code, out = _cli(["--text"], path=str(folder))
        check("no claude: a sentence, not a traceback", code == 2 and "npm install" in out and "Traceback" not in out, out[-120:])
        stub = _stub_path(folder)
        code, out = _cli(["--text", "--cwd", "/no/such/folder"], path=stub)
        check("bad --cwd: a sentence", code == 2 and "isn't a folder" in out and "Traceback" not in out)
        code, out = _cli(["haiku", "--text"], path=stub)
        check("stray word: asks what you meant", code == 2 and "--model haiku" in out)
        code, out = _cli(["--text", "--", "--some-claude-flag"], path=stub)
        check("flags after -- still pass through", code == 0, out[-120:])
        code, out = _cli(["--vad", "webrtc", "--whisper-model", "bass.en"], path=stub)
        check("bad whisper model stops before any audio", code == 2 and "Traceback" not in out, out[-140:])
    if sys.platform != "win32":
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory() as tmp:
            stub = _stub_path(Path(tmp))
            code, out = _cli(["--text", "--sandbox"], path=stub, cwd=home)
            check("--sandbox from home is refused", code == 2 and ("cd into a project" in out or "--sandbox" in out), out[-120:])


def test_busy_phone_port() -> None:
    """Fails at the bind, ahead of any device, so it's safe on a machine with real audio."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            stub = _stub_path(Path(tmp))
            code, out = _cli(["--phone", "--phone-port", str(port), "--tts", "null", "--no-speech-check"], path=stub)
            check("busy phone port: a sentence", code == 2 and "--phone-port" in out and "Traceback" not in out, out[-160:])
    finally:
        sock.close()


async def test_no_audio_library() -> None:
    from talk2me import audio
    from talk2me.__main__ import _parse_args, _run_voice

    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"
    real_sd, real_which = audio.sd, preflight.agent_problem
    preflight.agent_problem = lambda _agent: None
    # With sd gone there's no device to touch, whatever machine this runs on.
    audio.sd = None
    try:
        cfg = _parse_args(["--tts", "null", "--no-speech-check"])
        code, _ = await _run_voice(cfg)
        check("no PortAudio: a sentence and exit 2", code == 2)
    finally:
        audio.sd, preflight.agent_problem = real_sd, real_which


async def test_ears_that_wont_load() -> None:
    import numpy as np

    from talk2me.config import Config
    from talk2me.orchestrator import Orchestrator
    from talk2me.orchestrator_errors import StartupError
    from talk2me.vad import EnergyVAD

    from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

    class Offline(FakeSTT):
        def warmup(self) -> None:
            raise OSError("no route to host")

    mic = FakeMic([np.zeros(480, dtype=np.float32)])
    backend = FakeBackend()
    orch = Orchestrator(
        cfg=Config(), backend=backend, vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=Offline([]), tts=FakeTTS(), mic=mic, speaker=FakeSpeaker(16000),
    )
    try:
        await asyncio.wait_for(orch.run(), 5)
        check("ears failing to load stop the session", False)
    except StartupError as exc:
        check("ears failing to load: a sentence", "connection" in str(exc), str(exc)[:80])
    check("and the mic is off again", mic.started is False)
    check("and the agent is closed", backend.closed)


def test_installer() -> None:
    if sys.platform == "win32":
        return
    script = Path(__file__).parent.parent / "install.sh"
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, timeout=30, check=False)
    check("installer has --help", out.returncode == 0 and "--ref" in out.stdout and "--whisper-gpu" in out.stdout)
    if not (sys.platform == "darwin" and os.uname().machine == "arm64"):
        out = subprocess.run(["bash", str(script), "--whisper-gpu"], capture_output=True, text=True, timeout=30, check=False)
        check("installer refuses GPU ears elsewhere", out.returncode == 1 and "Apple Silicon" in out.stdout)


def main() -> int:
    test_checks()
    test_engines_that_are_gone()
    test_real_cli()
    test_busy_phone_port()
    asyncio.run(test_no_audio_library())
    asyncio.run(test_ears_that_wont_load())
    test_installer()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

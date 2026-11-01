"""The GPU ears, with a fake mlx_whisper. No MLX, no model, no audio device, so
it runs in CI on Linux. What's locked: the lazy contract, one worker thread for
everything, what reaches the decoder, the silence guard, the first-run download
notice, and every road to the CPU fallback.

Run:  ./.venv/bin/python -m tests.test_mlx_whisper
"""

import asyncio
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np

from talk2me.config import Config
from talk2me.orchestrator import Orchestrator
from talk2me.protocols import STT
from talk2me.stt.mlx_whisper import _BASE_PROMPT, _MAX_PROMPT_CHARS, MLXWhisperSTT
from talk2me.vad import EnergyVAD

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

RESULTS: list[bool] = []
AUDIO = np.full(16000, 0.1, dtype=np.float32)


def _report(group: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


class FakeMLX(ModuleType):
    """Stands in for the mlx_whisper module: records every call."""

    def __init__(self, text: str = " Hello there. ", fail_on: int | None = None) -> None:
        super().__init__("mlx_whisper")
        self.calls: list[dict] = []
        self.threads: set[str] = set()
        self.text = text
        self.fail_on = fail_on

    def transcribe(self, audio, **kwargs):
        self.threads.add(threading.current_thread().name)
        self.calls.append({"audio": audio, **kwargs})
        if self.fail_on is not None and len(self.calls) >= self.fail_on:
            raise RuntimeError("[metal] out of memory")
        return {"text": self.text}


class FakeFallback:
    def __init__(self) -> None:
        self.warmed = False
        self.context: list[str] = []
        self.got: list[np.ndarray] = []

    def warmup(self) -> None:
        self.warmed = True

    def set_context(self, terms: list[str]) -> None:
        self.context = terms

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        self.got.append(audio)
        return "from the cpu."


def _engine(fake: ModuleType | None, *, speech: bool = True, **kwargs) -> MLXWhisperSTT:
    """An engine wired to the fake, skipping the Hub and the real VAD."""
    sys.modules["mlx_whisper"] = fake  # None makes `import mlx_whisper` raise
    stt = MLXWhisperSTT(**kwargs)
    stt._resolve_model = lambda: "/fake/model"
    real_ensure = stt._ensure_model

    def ensure():
        mlx = real_ensure()
        stt._vad = SimpleNamespace(get_speech_timestamps=lambda _a: [{}] if speech else [])
        return mlx

    stt._ensure_model = ensure
    return stt


def test_lazy_contract() -> None:
    sys.modules.pop("mlx_whisper", None)
    stt = MLXWhisperSTT()
    _report(
        "construction loads nothing",
        isinstance(stt, STT) and stt._mlx is None and "mlx_whisper" not in sys.modules,
    )


async def test_decode_arguments() -> None:
    fake = FakeMLX()
    stt = _engine(fake, vocab=["Lorecraft"])
    stt.warmup()
    warm_calls = len(fake.calls)
    stt.set_context(["useAuthStore", "lorecraft", "magma.js"])
    text = await stt.transcribe(AUDIO, 16000)
    call = fake.calls[-1]
    _report("warmup decodes once, so the first utterance is warm", warm_calls == 1)
    _report("transcript stripped", text == "Hello there.")
    _report("numpy array passed straight in, no temp WAV", isinstance(call["audio"], np.ndarray))
    _report(
        "decode pinned",
        call["temperature"] == 0.0
        and call["condition_on_previous_text"] is False
        and call["language"] == "en"
        and call["path_or_hf_repo"] == "/fake/model",
    )
    _report(
        "vocab + context reach initial_prompt, deduped, static first",
        call["initial_prompt"] == f"{_BASE_PROMPT} Terms: Lorecraft, useAuthStore, magma.js.",
        call["initial_prompt"],
    )
    _report("all MLX work on one thread", fake.threads == {"mlx-whisper_0"}, str(fake.threads))


async def test_prompt_shape() -> None:
    bare = _engine(FakeMLX())
    _report("no terms still gets the punctuated base prompt", bare._initial_prompt() == _BASE_PROMPT)
    big = _engine(FakeMLX(), vocab=["keep-me"] + [f"term{i:04d}" for i in range(500)])
    prompt = big._initial_prompt()
    _report(
        "prompt capped, static vocab survives",
        len(prompt) <= _MAX_PROMPT_CHARS + 100 and "keep-me" in prompt,
        f"{len(prompt)} chars",
    )


async def test_languages_and_rates() -> None:
    fake = FakeMLX()
    stt = _engine(fake, language="auto")
    await stt.transcribe(AUDIO, 16000)
    _report("auto detects per utterance", fake.calls[-1]["language"] is None)
    # Warmup needs SOME language to decode silence without a detection pass.
    _report("warmup decode never detects", fake.calls[0]["language"] == "en")
    fake = FakeMLX()
    stt = _engine(fake, language="es")
    await stt.transcribe(AUDIO, 16000)
    _report("explicit language passes through", fake.calls[-1]["language"] == "es")
    await stt.transcribe(np.zeros(48000, dtype=np.float32) + 0.1, 48000)
    _report("48 kHz resampled to 16 kHz", fake.calls[-1]["audio"].shape[0] == 16000)


async def test_silence_guard() -> None:
    fake = FakeMLX(text=" Thank you.")
    stt = _engine(fake, speech=False)
    stt.warmup()
    calls = len(fake.calls)
    text = await stt.transcribe(AUDIO, 16000)
    _report("no speech found -> empty, decoder never runs", text == "" and len(fake.calls) == calls)


def _fake_hub(*, cached_dir: Path | None, downloaded: Path, log: list[str]) -> None:
    hub = ModuleType("huggingface_hub")

    def snapshot_download(repo, local_files_only=False):
        log.append("cache" if local_files_only else "download")
        if local_files_only:
            if cached_dir is None:
                raise OSError("not in cache")
            return str(cached_dir)
        return str(downloaded)

    hub.snapshot_download = snapshot_download
    sys.modules["huggingface_hub"] = hub


def test_download_notice() -> None:
    real = sys.modules.get("huggingface_hub")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            complete = Path(tmp, "complete")
            partial = Path(tmp, "partial")
            complete.mkdir()
            partial.mkdir()
            (complete / "weights.safetensors").touch()
            fresh = Path(tmp, "fresh")

            def resolve(cached):
                log: list[str] = []
                notes: list[str] = []
                _fake_hub(cached_dir=cached, downloaded=fresh, log=log)
                stt = MLXWhisperSTT()
                stt.set_notifier(notes.append)
                return stt._resolve_model(), log, notes

            path, log, notes = resolve(complete)
            _report(
                "cached model: silent, no network",
                path == str(complete) and log == ["cache"] and notes == [],
            )
            path, log, notes = resolve(None)
            _report(
                "first run: says so BEFORE the long download",
                path == str(fresh)
                and log == ["cache", "download"]
                and len(notes) == 1
                and "1.6 GB" in notes[0],
                notes[0] if notes else "",
            )
            path, log, notes = resolve(partial)
            _report(
                "interrupted download (no weights) resumes",
                path == str(fresh) and log == ["cache", "download"],
            )
    finally:
        if real is None:
            sys.modules.pop("huggingface_hub", None)
        else:
            sys.modules["huggingface_hub"] = real


async def test_fallback_when_import_fails() -> None:
    cpu = FakeFallback()
    notes: list[str] = []
    stt = _engine(None, fallback=lambda: cpu, vocab=["x"])
    stt.set_notifier(notes.append)
    stt.set_context(["useAuthStore"])
    stt.warmup()
    _report("import failure lands on the fallback", stt.engine_name == "whisper" and cpu.warmed)
    _report("one plain notice", len(notes) == 1 and "CPU" in notes[0], notes[0] if notes else "")
    _report("live context carried over", cpu.context == ["useAuthStore"])
    text = await stt.transcribe(AUDIO, 16000)
    stt.set_context(["later"])
    _report(
        "transcribes on the fallback from then on",
        text == "from the cpu." and cpu.context == ["later"],
    )


async def test_fallback_mid_session() -> None:
    cpu = FakeFallback()
    notes: list[str] = []
    fake = FakeMLX(fail_on=2)  # warmup fine, first real decode dies
    stt = _engine(fake, fallback=lambda: cpu)
    stt.set_notifier(notes.append)
    stt.warmup()
    _report("healthy after warmup", stt.engine_name == "mlx-whisper")
    text = await stt.transcribe(AUDIO, 16000)
    _report(
        "a Metal error mid-session keeps the utterance",
        text == "from the cpu." and len(cpu.got) == 1 and stt.engine_name == "whisper",
    )
    _report("mid-session notice", len(notes) == 1)


async def test_no_fallback_raises() -> None:
    stt = _engine(None)
    try:
        stt.warmup()
    except ImportError:
        _report("no fallback wired: the failure surfaces", True)
    else:
        _report("no fallback wired: the failure surfaces", False)


class NoticingSTT(FakeSTT):
    """The duck-typed seams the orchestrator looks for."""

    engine_name = "whisper"

    def __init__(self, transcripts: list[str]) -> None:
        super().__init__(transcripts)
        self.notifier = None

    def set_notifier(self, notify) -> None:
        self.notifier = notify

    def warmup(self) -> None:
        self.notifier("downloading, hang on")


async def test_orchestrator_wiring() -> None:
    cfg = Config(stt="mlx-whisper")
    stt = NoticingSTT([])
    orch = Orchestrator(
        cfg=cfg,
        backend=FakeBackend([]),
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=stt,
        tts=FakeTTS(),
        mic=FakeMic([], sample_rate=16000),
        speaker=FakeSpeaker(16000),
    )
    notes: list[str] = []
    orch.render.status_note = notes.append
    await asyncio.wait_for(orch.run(), timeout=10)
    _report("orchestrator hands the engine a notifier", notes == ["downloading, hang on"], str(notes))
    _report("startup card reports the engine really running", cfg.stt == "whisper")


async def main_async() -> None:
    await test_decode_arguments()
    await test_prompt_shape()
    await test_languages_and_rates()
    await test_silence_guard()
    await test_fallback_when_import_fails()
    await test_fallback_mid_session()
    await test_no_fallback_raises()
    await test_orchestrator_wiring()


def main() -> int:
    test_lazy_contract()
    test_download_notice()
    asyncio.run(main_async())
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)} passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

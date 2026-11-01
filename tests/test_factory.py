"""Provider-construction test for talk2me.factory.

Pure-logic, no audio, no model load, no subprocess. We construct providers off
Config and assert the factory wires the right concrete class for each engine
string, raises on unknown strings, computes frame_samples correctly, and builds
the whisper STT / claude backend lazily (type/attrs only — no model download, no
`claude` spawn). Runtime-checkable Protocols are asserted with isinstance where
they add signal.

Run:  ./.venv/bin/python -m tests.test_factory
"""

from __future__ import annotations

import os

from talk2me import factory
from talk2me.backends import ClaudeCodeBackend
from talk2me.config import Config
from talk2me.factory import (
    build_backend,
    build_stt,
    build_tts,
    build_vad,
    frame_samples,
)
from talk2me.orchestrator import _context_terms
from talk2me.protocols import STT, TTS, VAD, AgentBackend
from talk2me.stt import WhisperSTT
from talk2me.tts import SayTTS
from talk2me.tts.null import NullTTS
from talk2me.vad import EnergyVAD


def _line(name: str, ok: bool, detail: str = "") -> bool:
    suffix = f"  {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    return ok


def _raises_value_error(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:  # noqa: BLE001 — wrong exception type is a fail, not a crash
        return False
    return False


def test_build_tts() -> bool:
    say = build_tts(Config(tts="say"))
    null = build_tts(Config(tts="null"))
    ok = (
        isinstance(say, SayTTS)
        and isinstance(say, TTS)  # runtime Protocol check
        and isinstance(null, NullTTS)
        and isinstance(null, TTS)
        and _raises_value_error(lambda: build_tts(Config(tts="bogus")))
    )
    return _line(
        "build_tts",
        ok,
        f"say={type(say).__name__} null={type(null).__name__} bogus->ValueError",
    )


def test_build_vad() -> bool:
    vad = build_vad(Config(vad="energy"))
    expected_fs = int(16000 * 30 / 1000)  # 480
    ok = (
        isinstance(vad, EnergyVAD)
        and isinstance(vad, VAD)  # runtime Protocol check
        and expected_fs == 480
        and vad.frame_samples == expected_fs
        and vad.sample_rate == 16000
        and _raises_value_error(lambda: build_vad(Config(vad="bogus")))
    )
    return _line(
        "build_vad",
        ok,
        f"energy={type(vad).__name__} frame_samples={vad.frame_samples} "
        f"(expected {expected_fs}) bogus->ValueError",
    )


def test_build_stt() -> bool:
    # Construction is lazy: WhisperSTT.__init__ must NOT load the model. We check
    # type + attrs only and never call transcribe(), so no model is downloaded.
    stt = build_stt(Config())
    vocab_stt = build_stt(Config(vocab=["Lorecraft", "Morgen"]))
    ok = (
        isinstance(stt, WhisperSTT)
        and isinstance(stt, STT)  # runtime Protocol check (transcribe present)
        and stt._model is None  # lazy: nothing loaded on construction
        and stt._hotwords() is None  # default empty vocab -> no biasing
        and vocab_stt._model is None
        and vocab_stt._hotwords() == "Lorecraft, Morgen"
    )
    return _line(
        "build_stt",
        ok,
        f"stt={type(stt).__name__} model_loaded={stt._model is not None} "
        f"hotwords={vocab_stt._hotwords()!r}",
    )


def test_build_backend() -> bool:
    # Must build the backend WITHOUT starting it: no `claude` subprocess spawned.
    # ClaudeCodeBackend.start() is async; merely constructing leaves _proc None.
    backend = build_backend(Config(model="haiku"))
    from talk2me.config import VOICE_SYSTEM_PROMPT

    # Defaults: user config isolated, voice persona appended in voice mode.
    text_backend = build_backend(Config(input_mode="text"))
    full_backend = build_backend(Config(with_user_config=True))
    ok = (
        isinstance(backend, ClaudeCodeBackend)
        and isinstance(backend, AgentBackend)  # runtime Protocol check
        and backend._model == "haiku"
        and backend._proc is None  # not started
        and backend._setting_sources == "project,local"
        and backend._append_system_prompt == VOICE_SYSTEM_PROMPT
        and text_backend._append_system_prompt is None
        and full_backend._setting_sources is None
    )
    return _line(
        "build_backend",
        ok,
        f"backend={type(backend).__name__} model={backend._model!r} "
        f"sources={backend._setting_sources!r} "
        f"voice_prompt={'yes' if backend._append_system_prompt else 'no'}",
    )


def test_build_stt_parakeet() -> bool:
    # Lazy contract: constructing the parakeet engine must not import
    # parakeet_mlx (CI has no MLX), and satisfies the STT Protocol.
    import sys

    stt = build_stt(Config(stt="parakeet"))
    from talk2me.stt.parakeet import ParakeetMLXSTT

    ok = (
        isinstance(stt, ParakeetMLXSTT)
        and isinstance(stt, STT)
        and stt._model is None
        and "parakeet_mlx" not in sys.modules
        and _raises_value_error(lambda: build_stt(Config(stt="bogus")))
    )
    return _line(
        "build_stt parakeet",
        ok,
        f"stt={type(stt).__name__} mlx_imported={'parakeet_mlx' in sys.modules}",
    )


def test_build_stt_mlx_whisper() -> bool:
    # Same lazy contract as parakeet: CI has no MLX, and building must not care.
    import sys

    stt = build_stt(Config(stt="whisper-gpu", vocab=["Lorecraft"], language="es"))
    from talk2me.stt.mlx_whisper import MLXWhisperSTT

    ok = (
        isinstance(stt, MLXWhisperSTT)
        and isinstance(stt, STT)
        and stt._mlx is None
        and "mlx_whisper" not in sys.modules
        and stt._vocab == ["Lorecraft"]
        and stt._language == "es"
        and isinstance(stt._make_fallback(), WhisperSTT)
    )
    return _line("build_stt whisper-gpu", ok, f"stt={type(stt).__name__}")


def test_resolve_stt() -> bool:
    real = factory.whisper_gpu_supported, factory.whisper_gpu_installed

    def on(supported: bool, installed: bool) -> None:
        factory.whisper_gpu_supported = lambda: supported
        factory.whisper_gpu_installed = lambda: installed

    try:
        on(True, True)
        ready = factory.resolve_stt("whisper-gpu")
        whisper = factory.resolve_stt("whisper")
        parakeet = factory.resolve_stt("parakeet")
        on(True, False)
        missing = factory.resolve_stt("whisper-gpu")
        on(False, False)
        elsewhere = factory.resolve_stt("whisper-gpu")
    finally:
        factory.whisper_gpu_supported, factory.whisper_gpu_installed = real
    ok = (
        ready == ("whisper-gpu", None)
        and whisper == ("whisper", None)
        and parakeet == ("parakeet", None)
        and missing[0] == "whisper" and "pip install mlx-whisper" in missing[1]
        and elsewhere[0] == "whisper" and "Apple Silicon" in elsewhere[1]
    )
    return _line("resolve_stt", ok, f"ready={ready} missing={missing[0]}")


def test_default_ears() -> bool:
    """base.en on the CPU is the default, everywhere, with no new download."""
    import sys
    import tomllib
    from pathlib import Path

    from talk2me.__main__ import _parse_args

    real = factory.whisper_gpu_supported, factory.whisper_gpu_installed
    factory.whisper_gpu_supported = lambda: True
    factory.whisper_gpu_installed = lambda: True
    try:
        cfg = _parse_args([])
    finally:
        factory.whisper_gpu_supported, factory.whisper_gpu_installed = real
    stt = build_stt(cfg)
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    ok = (
        # Even on Apple Silicon with the package present, you have to ask.
        cfg.stt == "whisper"
        and cfg.whisper_model == "base.en"
        and cfg.stt_notice is None
        and isinstance(stt, WhisperSTT)
        and stt._model_name == "base.en"
        and "mlx_whisper" not in sys.modules
        and not any("mlx" in dep for dep in pyproject["dependencies"])
        and "whisper-gpu" in pyproject["optional-dependencies"]
    )
    return _line("default ears", ok, f"stt={cfg.stt} model={cfg.whisper_model}")


def test_hotword_context() -> bool:
    # Live context merges after static vocab, deduped case-insensitively.
    stt = WhisperSTT(vocab=["Lorecraft"])
    stt.set_context(["useAuthStore", "lorecraft", "magma.js"])
    merged = stt._hotwords()
    # Identifier-ish extraction from agent prose.
    terms = _context_terms(
        "I updated useAuthStore in magma.js and the sync_helpers module for Morgen."
    )
    ok = (
        merged == "Lorecraft, useAuthStore, magma.js"
        and "useAuthStore" in terms
        and "magma.js" in terms
        and "sync_helpers" in terms
        and "Morgen" in terms
    )
    return _line(
        "hotword context seeding",
        ok,
        f"merged={merged!r} terms={terms}",
    )


def test_parse_permission_posture() -> bool:
    from talk2me.__main__ import _parse_args
    from talk2me.config import DEFAULT_DISALLOWED_TOOLS

    default = _parse_args([])
    gated = _parse_args(["--gated"])
    auto = _parse_args(["--auto-approve"])
    text = _parse_args(["--text"])
    text_gated = _parse_args(["--text", "--gated"])
    ok = (
        # A mic is not a keyboard: voice asks first unless told otherwise.
        default.permission_mode == "default"
        and default.voice_approval is True
        and gated.permission_mode == "default"
        and gated.voice_approval is True
        and auto.permission_mode == "bypassPermissions"
        and auto.voice_approval is False
        # The denylist survives bypass — it's the load-bearing guardrail.
        and next(iter(DEFAULT_DISALLOWED_TOOLS)) in auto.disallowed_tools
        # Typed input keeps its old default; the flag still turns the gate on.
        and text.permission_mode == "bypassPermissions"
        and text_gated.voice_approval is True
    )
    return _line(
        "parse permission posture",
        ok,
        f"default={default.permission_mode}/gate={default.voice_approval} "
        f"auto={auto.permission_mode} text={text.permission_mode}",
    )


def test_denylist_shape() -> bool:
    from talk2me.config import DEFAULT_DISALLOWED_TOOLS

    # The real matching proof is tests/manual_denylist_check.py, against the
    # CLI. Here: the rules the audit named are present, and none can break the
    # comma-joined --disallowedTools argument.
    must_have = (
        "Bash(rm -rf:*)", "Bash(rm -fr*)", "Bash(*/rm *)", "Bash(find * -delete*)",
        "Bash(python3 -c*)", "Bash(node -e*)", "Bash(bash -c*)", "Bash(scp:*)",
        "Bash(nc:*)", "Bash(*/curl *)", "Bash(git -* push*)",
    )
    rules = DEFAULT_DISALLOWED_TOOLS
    ok = (
        all(r in rules for r in must_have)
        and not any("," in r for r in rules)
        and len(rules) == len(set(rules))
    )
    return _line("denylist shape", ok, f"{len(rules)} rules")


def test_frame_samples() -> bool:
    fs = frame_samples(Config())
    ok = fs == 480
    return _line("frame_samples", ok, f"frame_samples={fs} (expected 480)")


def main() -> int:
    # Tests must never read a real ~/.talk2me/config.json: _parse_args loads it.
    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"
    results = [
        test_build_tts(),
        test_build_vad(),
        test_build_stt(),
        test_build_backend(),
        test_build_stt_parakeet(),
        test_build_stt_mlx_whisper(),
        test_resolve_stt(),
        test_default_ears(),
        test_hotword_context(),
        test_parse_permission_posture(),
        test_denylist_shape(),
        test_frame_samples(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall: "
          f"{sum(results)}/{len(results)} groups passed")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

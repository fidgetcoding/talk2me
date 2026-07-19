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
    ok = (
        isinstance(backend, ClaudeCodeBackend)
        and isinstance(backend, AgentBackend)  # runtime Protocol check
        and backend._model == "haiku"
        and backend._proc is None  # not started
    )
    return _line(
        "build_backend",
        ok,
        f"backend={type(backend).__name__} model={backend._model!r} "
        f"started={backend._proc is not None}",
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


def test_frame_samples() -> bool:
    fs = frame_samples(Config())
    ok = fs == 480
    return _line("frame_samples", ok, f"frame_samples={fs} (expected 480)")


def main() -> int:
    results = [
        test_build_tts(),
        test_build_vad(),
        test_build_stt(),
        test_build_backend(),
        test_build_stt_parakeet(),
        test_hotword_context(),
        test_frame_samples(),
    ]
    overall = all(results)
    print(f"[{'PASS' if overall else 'FAIL'}] overall: "
          f"{sum(results)}/{len(results)} groups passed")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())

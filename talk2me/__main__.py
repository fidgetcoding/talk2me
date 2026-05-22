"""CLI entrypoint. `python -m talk2me [flags]`.

Maps argv onto Config, builds the provider stack, runs the orchestrator. A
`--text` mode skips all audio (type instead of talk) for cheap loop testing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import factory
from .config import Config


def _parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(prog="talk2me", description=__doc__)
    p.add_argument("--model", default=None, help="claude model (e.g. haiku, sonnet)")
    p.add_argument("--cwd", default=None, help="working dir for the agent")
    p.add_argument("--permission-mode", default="default")
    p.add_argument("--text", action="store_true", help="type instead of talk (no audio)")
    p.add_argument("--whisper-model", default="base.en")
    p.add_argument("--tts", default="say", choices=["say", "null"])
    p.add_argument("--voice", default=None, help="`say` voice id")
    p.add_argument("--energy-threshold", type=float, default=0.012)
    p.add_argument("--silence-ms", type=int, default=900)
    p.add_argument(
        "--vocab", action="append", default=[],
        help="bias term for STT (repeatable): --vocab Lorecraft --vocab Morgen",
    )
    p.add_argument(
        "--vocab-file", default=None,
        help="file of bias terms, one per line or comma-separated",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="print VAD speech/turn transitions (for tuning --energy-threshold)",
    )
    p.add_argument("claude_args", nargs="*", help="extra args passed to `claude`")
    a = p.parse_args(argv)
    return Config(
        debug=a.debug,
        model=a.model,
        cwd=a.cwd,
        permission_mode=a.permission_mode,
        input_mode="text" if a.text else "voice",
        whisper_model=a.whisper_model,
        tts=a.tts,
        voice=a.voice,
        energy_threshold=a.energy_threshold,
        silence_ms=a.silence_ms,
        vocab=_collect_vocab(a.vocab, a.vocab_file),
        extra_claude_args=a.claude_args,
    )


def _collect_vocab(terms: list[str], path: str | None) -> list[str]:
    out = list(terms)
    if path:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                out += [t.strip() for t in line.split(",") if t.strip()]
    return out


async def _run_voice(cfg: Config) -> int:
    from .audio import Mic, Speaker
    from .orchestrator import Orchestrator

    tts = factory.build_tts(cfg)
    orch = Orchestrator(
        cfg=cfg,
        backend=factory.build_backend(cfg),
        vad=factory.build_vad(cfg),
        stt=factory.build_stt(cfg),
        tts=tts,
        mic=Mic(cfg.sample_rate, factory.frame_samples(cfg)),
        speaker=Speaker(tts.sample_rate),
    )
    await orch.run()
    return 0


async def _run_text(cfg: Config) -> int:
    """Audio-free loop: read stdin lines, print agent text, no TTS/STT."""
    from .events import (
        AssistantTextDelta,
        BackendError,
        ToolActivity,
        TurnComplete,
    )

    backend = factory.build_backend(cfg)
    await backend.start()
    events = backend.events()
    print("talk2me (text mode) — type a message, Ctrl-D to quit.\n", flush=True)
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            await backend.send(line)
            async for ev in events:
                if isinstance(ev, AssistantTextDelta):
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
                elif isinstance(ev, ToolActivity):
                    print(f"\n[tool] {ev.name}", flush=True)
                elif isinstance(ev, TurnComplete):
                    print("\n", flush=True)
                    break
                elif isinstance(ev, BackendError):
                    print(f"\n[error] {ev.message}", flush=True)
                    break
    finally:
        await backend.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    cfg = _parse_args(argv if argv is not None else sys.argv[1:])
    runner = _run_text if cfg.input_mode == "text" else _run_voice
    try:
        return asyncio.run(runner(cfg))
    except KeyboardInterrupt:
        print("\nbye.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Do the speech engines leak over a long session? From files, no mic, no speakers:
hundreds of real-speech clips in a row, watching the process's memory. CPython's
tracemalloc can't see into CTranslate2, ONNX or MLX, so this asks the OS.

Run:  python -m tests.manual_long_ears --data <folder with LibriSpeech/dev-clean> [--clips 400]
"""

import argparse
import asyncio
import glob
import os
import random
import resource
import subprocess
import sys
from pathlib import Path

from talk2me.stt import WhisperSTT
from talk2me.stt.mlx_whisper import MLXWhisperSTT

from .manual_real_speech_bench import decode_audio


def _rss_mb() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True, check=False)
    return int(out.stdout.strip() or 0) / 1024


async def _run(label: str, stt, clips: list) -> None:
    stt.warmup()
    marks = []
    for i, audio in enumerate(clips, 1):
        await stt.transcribe(audio, 16000)
        if i % (len(clips) // 4) == 0:
            marks.append(f"{i}: {_rss_mb():.0f} MB")
    print(f"{label}: {', '.join(marks)}; peak {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * (1024 if sys.platform == 'darwin' else 1)):.0f} MB", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--clips", type=int, default=400)
    parser.add_argument("--engine", choices=["whisper", "whisper-gpu"], default="whisper")
    args = parser.parse_args()
    files = sorted(glob.glob(str(Path(args.data).expanduser() / "libri" / "LibriSpeech" / "dev-clean" / "*" / "*" / "*.flac")))
    random.Random(7).shuffle(files)
    clips = [decode_audio(f)[:16000 * 8] for f in files[: args.clips // 4]]
    clips = (clips * 4)[: args.clips]
    if args.engine == "whisper":
        await _run("whisper base.en, CPU", WhisperSTT(), clips)
    else:
        await _run("large-v3-turbo, GPU", MLXWhisperSTT(), clips)


if __name__ == "__main__":
    asyncio.run(main())

"""Race the ears on real audio, from files. Mac only, no mic, no speakers.

macOS `say` renders coding sentences straight into WAV files, then each engine
transcribes them through the same class the app uses. That makes the latency
real and the accuracy only a hint: these are synthetic voices, not you mumbling
at a laptop. Word errors also score "use AuthStore" as a miss, so read the
transcripts too. Engines that aren't installed get skipped. The first run of each
downloads its model.

Run:  python -m tests.manual_stt_bench         (add --cpu-turbo for the slow one)
"""

import asyncio
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from talk2me.stt import WhisperSTT
from talk2me.stt.mlx_whisper import MLXWhisperSTT
from talk2me.stt.parakeet import ParakeetMLXSTT

SENTENCES = [
    "Refactor useAuthStore so the orchestrator stops polling every thirty seconds.",
    "Open orchestrator.py and rename the speech check flag to no speech check.",
    "No wait, make that line fifty, then run pytest on the echo gate tests.",
    "Push the branch, then deploy the Supabase edge function to production.",
    "Why does the Kubernetes pod crash when the Redis connection times out?",
    "Add a TypeScript interface for the webhook payload and validate it with Zod.",
    "Summarize what changed in the last three commits and tell me what looks risky.",
    "Switch the default model to large v3 turbo and compare the latency on my Mac.",
]
VOICES = ["Ava (Premium)", "Samantha", "Daniel"]
# A prompt that knows the test's words: the best case, not the typical one.
VOCAB = ["useAuthStore", "orchestrator.py", "pytest", "Supabase", "Kubernetes", "Zod", "TypeScript"]


def _render(folder: str) -> list[tuple[str, np.ndarray]]:
    clips = []
    for voice in VOICES:
        for i, sentence in enumerate(SENTENCES):
            aiff = Path(folder, f"{i}-{voice[:3]}.aiff")
            wav = aiff.with_suffix(".wav")
            try:
                subprocess.run(["say", "-v", voice, "-o", str(aiff), sentence], check=True)
            except subprocess.CalledProcessError:
                print(f"(voice not installed, skipping: {voice})")
                break
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
                check=True,
            )
            with wave.open(str(wav)) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            clips.append((sentence, pcm.astype(np.float32) / 32768))
    return clips


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", "", text.lower().replace("-", " ")).split()


def _edits(ref: list[str], hyp: list[str]) -> int:
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


async def _race(label: str, stt, clips: list[tuple[str, np.ndarray]]) -> None:
    try:
        stt.warmup()
    except Exception as exc:  # noqa: BLE001 — a missing engine is a skip, not a crash
        print(f"{label}: skipped ({type(exc).__name__}: {str(exc)[:70]})")
        return
    edits = words = 0
    times = []
    misses = []
    for sentence, audio in clips:
        start = time.monotonic()
        text = await stt.transcribe(audio, 16000)
        times.append(time.monotonic() - start)
        wrong = _edits(_words(sentence), _words(text))
        edits += wrong
        words += len(_words(sentence))
        if wrong and len(misses) < 4:
            misses.append(text)
    print(
        f"{label}: median {np.median(times):.2f}s, worst {max(times):.2f}s, "
        f"word errors {100 * edits / words:.1f}%"
    )
    for text in misses:
        print(f"    {text}")


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        clips = _render(folder)
    print(f"{len(clips)} clips, {sum(len(c[1]) for c in clips) / 16000:.0f} s of speech")
    await _race("whisper base.en, CPU", WhisperSTT(), clips)
    if "--cpu-turbo" in sys.argv:
        await _race("whisper large-v3-turbo, CPU", WhisperSTT(model="large-v3-turbo"), clips)
    await _race("large-v3-turbo, GPU", MLXWhisperSTT(), clips)
    await _race("large-v3-turbo, GPU, prompt knows the words", MLXWhisperSTT(vocab=VOCAB), clips)
    await _race("parakeet v2, GPU", ParakeetMLXSTT(), clips)


if __name__ == "__main__":
    asyncio.run(main())

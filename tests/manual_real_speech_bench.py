"""The ears on recordings of real people, from files. No mic, no speakers.

Two openly licensed sets, both CC BY 4.0, downloaded on first run (not in this repo):

- LibriSpeech dev-clean (Panayotov et al., openslr.org/12): 40 volunteers reading
  audiobooks. Real voices and accents, quiet rooms, long sentences. Nothing like
  coding talk, so it tests the ears and not the jargon.
- Speech Commands test set v0.02 (Warden, Google, 2018): thousands of people saying
  one word into their own laptop or phone. That's the closest open data to the
  spoken approval gate, so it's scored on what the gate would have done.

Run:  python -m tests.manual_real_speech_bench --data ~/somewhere [--per-speaker 5] [--per-word 100]
"""

import argparse
import asyncio
import glob
import os
import random
import re
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from talk2me.orchestrator import match_intent
from talk2me.speechcheck import SileroSpeechCheck
from talk2me.stt import WhisperSTT
from talk2me.stt.mlx_whisper import MLXWhisperSTT
from talk2me.stt.parakeet import ParakeetMLXSTT

LIBRISPEECH = "https://www.openslr.org/resources/12/dev-clean.tar.gz"
COMMANDS = "http://download.tensorflow.org/data/speech_commands_test_set_v0.02.tar.gz"
GATE_WORDS = {"yes": "approve", "go": "approve", "no": "deny", "stop": "deny"}
OTHER_WORDS = ("up", "down", "left", "right", "on", "off")


def decode_audio(path):
    """16 kHz mono float32, through PyAV (faster-whisper's own dependency)."""
    import av

    frames = []
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                frames.append(out.to_ndarray().reshape(-1))
    return np.concatenate(frames).astype(np.float32) / 32768


def _fetch(url: str, folder: Path, marker: str) -> None:
    if (folder / marker).exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / "download.tar.gz"
    print(f"downloading {url}", flush=True)
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive) as tar:
        tar.extractall(folder, filter="data")
    archive.unlink()


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]", "", text.lower().replace("-", " ")).split()


def _edits(ref: list[str], hyp: list[str]) -> int:
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


def _librispeech(root: Path, per_speaker: int, seed: int) -> list[tuple[str, str, Path]]:
    clips = []
    rng = random.Random(seed)
    base = root / "LibriSpeech" / "dev-clean"
    for speaker in sorted(os.listdir(base)):
        lines = {}
        for trans in glob.glob(str(base / speaker / "*" / "*.trans.txt")):
            for line in Path(trans).read_text().splitlines():
                key, text = line.split(" ", 1)
                lines[key] = (text, Path(trans).parent / f"{key}.flac")
        keys = sorted(lines)
        rng.shuffle(keys)
        clips += [(speaker, lines[k][0], lines[k][1]) for k in keys[:per_speaker]]
    return clips


async def bench_sentences(label: str, stt, clips) -> None:
    stt.warmup()
    edits = words = 0
    times, ratios, speakers = [], [], {}
    for speaker, text, path in clips:
        audio = decode_audio(str(path))
        start = time.monotonic()
        out = await stt.transcribe(audio, 16000)
        elapsed = time.monotonic() - start
        times.append(elapsed)
        ratios.append(elapsed / (len(audio) / 16000))
        wrong = _edits(_words(text), _words(out))
        edits += wrong
        words += len(_words(text))
        spk = speakers.setdefault(speaker, [0, 0])
        spk[0] += wrong
        spk[1] += len(_words(text))
    per = sorted(100 * w / n for w, n in speakers.values())
    print(
        f"{label}: word errors {100 * edits / words:.1f}% over {words} words, "
        f"best speaker {per[0]:.1f}%, median {per[len(per) // 2]:.1f}%, worst {per[-1]:.1f}%, "
        f"median {np.median(times):.2f}s per clip ({np.median(ratios):.2f}x clip length)",
        flush=True,
    )


async def bench_gate(label: str, stt, root: Path, per_word: int, seed: int) -> None:
    stt.warmup()
    check = SileroSpeechCheck()
    rng = random.Random(seed)
    pad = np.zeros(8000, dtype=np.float32)

    def clips(word: str) -> list[str]:
        files = sorted(glob.glob(str(root / word / "*.wav")))
        rng.shuffle(files)
        return files[:per_word]

    async def verdict(path: str) -> tuple[str | None, bool]:
        audio = np.concatenate([pad, decode_audio(path), pad])
        if not check(audio, 16000):
            return None, False
        return match_intent(await stt.transcribe(audio, 16000)), True

    right = unclear = flipped = total = dropped = 0
    per_word_line = []
    for word, want in GATE_WORDS.items():
        word_right = 0
        files = clips(word)
        for path in files:
            got, passed = await verdict(path)
            total += 1
            dropped += not passed
            right += got == want
            word_right += got == want
            unclear += got is None
            flipped += got not in (None, want)
        per_word_line.append(f"{word} {100 * word_right / len(files):.0f}%")
    false_approve = other_total = false_deny = 0
    for word in OTHER_WORDS:
        for path in clips(word):
            got, _ = await verdict(path)
            other_total += 1
            false_approve += got == "approve"
            false_deny += got == "deny"
    print(
        f"{label}: right {100 * right / total:.1f}% of {total} ({', '.join(per_word_line)}), "
        f"unclear so it re-asks {100 * unclear / total:.1f}%, "
        f"OPPOSITE verdict {flipped} ({100 * flipped / total:.1f}%), dropped by the speech gate {dropped}; "
        f"on {other_total} other words: {false_approve} false approvals, {false_deny} false denials",
        flush=True,
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--per-speaker", type=int, default=5)
    parser.add_argument("--per-word", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--no-gpu", action="store_true")
    args = parser.parse_args()
    data = Path(args.data).expanduser()
    libri, words = data / "libri", data / "commands"
    _fetch(LIBRISPEECH, libri, "LibriSpeech")
    _fetch(COMMANDS, words, "yes")

    engines = [("whisper base.en, CPU", WhisperSTT)]
    if not args.no_gpu:
        engines += [("large-v3-turbo, GPU", MLXWhisperSTT), ("parakeet v2, GPU", ParakeetMLXSTT)]
    clips = _librispeech(libri, args.per_speaker, args.seed)
    print(f"LibriSpeech dev-clean: {len(clips)} clips, {len({c[0] for c in clips})} speakers, CC BY 4.0", flush=True)
    for label, make in engines:
        await bench_sentences(label, make(), clips)
    print(f"Speech Commands v0.02: {args.per_word} clips per word, real speakers, CC BY 4.0", flush=True)
    for label, make in engines:
        await bench_gate(label, make(), words, args.per_word, args.seed)


if __name__ == "__main__":
    asyncio.run(main())

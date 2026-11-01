"""Voice-lock on recordings of real people: does it keep the owner in and strangers
out? From files. No mic, no speakers, and it can't touch a real voiceprint.

LibriSpeech dev-clean (CC BY 4.0, 40 real speakers), each one enrolled through the
real VoiceLock.enroll against `say` voices as the agent's own voice, the way a real
enrollment calibrates. It runs twice:

- clean: the audiobook recordings as they are.
- laptop room: every clip through a simulated laptop mic: random room echo, a
  laptop's frequency response, fan-like noise at 10-25 dB, random level. It's a
  simulation, and it isn't a substitute for a real laptop in a real room.

Run:  python -m tests.manual_voicelock_bench --data ~/somewhere [--speakers 40]
"""

import argparse
import glob
import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from .manual_real_speech_bench import LIBRISPEECH, _fetch, decode_audio

SR = 16000
# Gain and speed per enrollment clip, standing in for the guided steps: normal,
# normal, louder, quieter, fast, slow, normal.
STYLES = [(1, 1), (1, 1), (2, 1), (0.25, 1), (1, 1.12), (1, 0.9), (1, 1)]
ENROLL_S = 5
TEST_LENGTHS = (1.0, 1.5, 2.0, 3.0, 4.0)


def laptop_room(audio: np.ndarray, rng: np.random.Generator, snr=None, rt60=None) -> np.ndarray:
    rt60 = rt60 or rng.uniform(0.25, 0.6)
    n = int(rt60 * SR)
    room = rng.normal(0, 1, n) * np.exp(-6.9 * np.arange(n) / n)
    room[0] = 3.0
    wet = np.fft.irfft(np.fft.rfft(audio, len(audio) + n) * np.fft.rfft(room, len(audio) + n))[: len(audio)]
    freqs = np.fft.rfftfreq(len(wet), 1 / SR)
    mic = np.clip((freqs - 120) / 180, 0, 1) * np.clip((7200 - freqs) / 800, 0, 1)
    mic = mic * (1 + 0.4 * np.exp(-(((freqs - 3200) / 900) ** 2)))
    wet = np.fft.irfft(np.fft.rfft(wet) * mic, len(wet))
    wet /= np.max(np.abs(wet)) + 1e-9
    fan = np.fft.irfft(np.fft.rfft(rng.normal(0, 1, len(wet))) / np.sqrt(np.maximum(freqs, 30)), len(wet))
    snr = snr if snr is not None else rng.uniform(10, 25)
    fan *= np.sqrt(np.mean(wet**2) / (np.mean(fan**2) + 1e-12)) * 10 ** (-snr / 20)
    return ((wet + fan) * rng.uniform(0.04, 0.3)).astype(np.float32)


def _stretch(audio: np.ndarray, speed: float) -> np.ndarray:
    n = int(len(audio) / speed)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def _cut(audio: np.ndarray, seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(seconds * SR)
    if len(audio) <= n:
        return audio
    start = rng.integers(0, len(audio) - n)
    return audio[start : start + n]


def _agent_voices() -> list[np.ndarray]:
    clips = []
    with tempfile.TemporaryDirectory() as tmp:
        for voice in ("Samantha", "Daniel"):
            for text in ("The quick brown fox jumps over the lazy dog while I test my voice.",
                         "Add the new feature, run the tests, and tell me when everything passes."):
                subprocess.run(["say", "-v", voice, "-o", f"{tmp}/a.aiff", text], check=True)
                subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", f"{tmp}/a.aiff", f"{tmp}/a.wav"], check=True)
                with wave.open(f"{tmp}/a.wav") as w:
                    clips.append(np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768)
    return clips


def _speakers(root: Path, limit: int, rng: np.random.Generator) -> dict[str, list[np.ndarray]]:
    out = {}
    for speaker in sorted(os.listdir(root / "LibriSpeech" / "dev-clean")):
        files = sorted(glob.glob(str(root / "LibriSpeech" / "dev-clean" / speaker / "*" / "*.flac")))
        if len(files) < 22:
            continue
        rng.shuffle(files)
        out[speaker] = [decode_audio(f) for f in files[:22]]
        if len(out) == limit:
            break
    return out


def _clips(speakers, room: bool, seed: int):
    """(enroll, test, agent) audio per condition. Same seed, same clips for each recipe."""
    rng = np.random.default_rng(seed)
    enroll, test = {}, {}
    for speaker, audio in speakers.items():
        setting = {"snr": rng.uniform(15, 25), "rt60": rng.uniform(0.25, 0.5)}
        takes = []
        for clip, (gain, speed) in zip(audio[:7], STYLES):
            take = np.clip(_stretch(_cut(clip, ENROLL_S, rng), speed) * gain, -1, 1).astype(np.float32)
            takes.append(laptop_room(take, rng, **setting) * gain if room else take)
        enroll[speaker] = takes
        probes = []
        for clip in audio[7:22]:
            cut = _cut(clip, float(rng.choice(TEST_LENGTHS)), rng)
            probes.append(laptop_room(cut, rng) if room else cut)
        test[speaker] = probes
    return enroll, test


def _eer(owner: np.ndarray, stranger: np.ndarray) -> float:
    points = np.sort(np.concatenate([owner, stranger]))
    rejected = np.searchsorted(np.sort(owner), points) / len(owner)
    accepted = 1 - np.searchsorted(np.sort(stranger), points) / len(stranger)
    i = np.argmin(np.abs(rejected - accepted))
    return float((rejected[i] + accepted[i]) / 2)


def _run(label: str, version: int, enroll, test, agent) -> None:
    from talk2me.voicelock import VoiceLock

    lock = VoiceLock()
    lock.features_version = version
    embedded = {s: np.stack([lock.embed(c) for c in clips]) for s, clips in test.items()}
    owners, strangers, rejected, accepted, enforcing = [], [], [], [], 0
    for speaker, takes in enroll.items():
        lock = VoiceLock()
        lock.enroll(takes, impostor_clips=agent, features_version=version)
        own = embedded[speaker] @ lock.voiceprint
        others = np.concatenate([embedded[s] @ lock.voiceprint for s in embedded if s != speaker])
        owners.append(own)
        strangers.append(others)
        if lock.meta["degraded"]:
            rejected.append(0.0)
            accepted.append(1.0)
        else:
            enforcing += 1
            rejected.append(float(np.mean(own < lock.threshold)))
            accepted.append(float(np.mean(others >= lock.threshold)))
    print(
        f"{label}: enforces for {enforcing}/{len(enroll)} speakers, owner rejected {100 * np.mean(rejected):.1f}%, "
        f"stranger let through {100 * np.mean(accepted):.1f}%, "
        f"equal-error rate {100 * _eer(np.concatenate(owners), np.concatenate(strangers)):.1f}%, "
        f"owner score {np.concatenate(owners).mean():.2f}, stranger {np.concatenate(strangers).mean():.2f}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--speakers", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    data = Path(args.data).expanduser() / "libri"
    _fetch(LIBRISPEECH, data, "LibriSpeech")
    # Never the real voiceprint: enroll() saves one per speaker, and they land here.
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_VOICEPRINT"] = f"{tmp}/voiceprint.npz"
        from talk2me.voicelock import ensure_model

        ensure_model()
        speakers = _speakers(data, args.speakers, np.random.default_rng(args.seed))
        voices = _agent_voices()
        print(f"{len(speakers)} real speakers, {len(voices)} clips of the agent's voice")
        for condition, room in (("clean", False), ("laptop room", True)):
            enroll, test = _clips(speakers, room, args.seed)
            rng = np.random.default_rng(args.seed)
            agent = [laptop_room(v, rng) if room else v for v in voices]
            _run(f"{condition}, before (no mean normalization)", 1, enroll, test, agent)
            _run(f"{condition}, after", 2, enroll, test, agent)


if __name__ == "__main__":
    main()

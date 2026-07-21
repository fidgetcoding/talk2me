"""Voice-lock — the mic answers to YOUR voice.

A speaker-embedding model (WeSpeaker CAM++, voxceleb, ONNX via the sherpa-onnx
model zoo — Apache-2.0, ~28 MB) turns an utterance into a 512-dim voiceprint.
Enrollment records you reading three sentences, averages the embeddings, and —
the part that makes a thin global margin workable — CALIBRATES the accept
threshold per install: it measures your self-similarity AND the similarity of
the agent's own TTS voice, then places the bar between them.

Recipe pinned by live spike (2026-07-19): kaldi-native-fbank 80-mel fbank on
int16-scale samples (model metadata: normalize_samples=0), dither 0, and
crucially NO cepstral mean normalization — CMN inverts this model's metric.

Utterances shorter than MIN_VERIFY_S embed unreliably and PASS unlocked (a
"wake up" must never be rejected); the Silero speech gate still applies.
Toggle live with "team session" (everyone talks) / "solo session" (locked).
"""

from __future__ import annotations

import json
import os
import urllib.request

import numpy as np

MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/wespeaker_en_voxceleb_CAM%2B%2B.onnx"
)  # yes, "recongition" — the tag is spelled that way upstream

MIN_VERIFY_S = 0.8  # below this, verification is unreliable -> pass-through
DEFAULT_THRESHOLD = 0.60
# Sanity bounds only — the calibrated midpoint must be free to follow the
# observed similarity scale (a tight ceiling once clamped the threshold
# BELOW the measured impostor, defeating the calibration entirely).
THRESHOLD_FLOOR, THRESHOLD_CEIL = 0.40, 0.90

# (style direction, sentence) — the styles matter as much as the words: a
# voiceprint built only from calm read-aloud clips doubts the same person
# loud, quiet, or fast. The last step is the actual command vocabulary, so
# wake/pause phrases are in-domain.
ENROLL_STEPS = (
    ("your normal voice",
     "The quick brown fox jumps over the lazy dog while I test my voice."),
    ("normal voice again",
     "Add the new feature, run the tests, and tell me when everything passes."),
    ("a bit LOUDER — like talking across the room",
     "Hey, stop what you're doing and open the browser for me."),
    ("QUIETER — like someone's sleeping nearby",
     "Wake up quietly and continue the previous session please."),
    ("fast — like you're excited about it",
     "Build it, test it, ship it, and tell me the moment it's done."),
    ("slow and relaxed",
     "Every project starts with one sentence spoken out loud, like this one."),
    ("your usual command voice",
     "Pause listening. Wake up. Team session. Solo session. Resume the task."),
)

# Plain texts (impostor synthesis + tests).
ENROLL_SENTENCES = tuple(s for _, s in ENROLL_STEPS)


def model_path() -> str:
    return os.environ.get("TALK2ME_VOICELOCK_MODEL") or os.path.expanduser(
        "~/.talk2me/models/voicelock.onnx"
    )


def voiceprint_path() -> str:
    return os.environ.get("TALK2ME_VOICEPRINT") or os.path.expanduser(
        "~/.talk2me/voiceprint.npz"
    )


def ensure_model(progress=print) -> str:
    """Download the embedding model once (~28 MB)."""
    path = model_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        progress("(downloading the voice-lock model — one time, ~28 MB…)")
        tmp = path + ".part"
        urllib.request.urlretrieve(MODEL_URL, tmp)
        os.replace(tmp, path)
    return path


class VoiceLock:
    """Embed, enroll, verify. Lazy model load; thread-safe after warmup."""

    def __init__(self, path: str | None = None) -> None:
        self._model_path = path or model_path()
        self._sess = None
        self.voiceprint: np.ndarray | None = None
        self.threshold: float = DEFAULT_THRESHOLD
        self.meta: dict = {}

    # ---- model -----------------------------------------------------------

    def _session(self):
        if self._sess is None:
            import onnxruntime as ort

            self._sess = ort.InferenceSession(
                self._model_path, providers=["CPUExecutionProvider"]
            )
        return self._sess

    def warmup(self) -> None:
        self.embed(np.zeros(16000, dtype=np.float32))

    @staticmethod
    def _fbank(audio: np.ndarray) -> np.ndarray:
        import kaldi_native_fbank as knf

        opts = knf.FbankOptions()
        opts.frame_opts.samp_freq = 16000
        opts.frame_opts.dither = 0.0
        opts.mel_opts.num_bins = 80
        fb = knf.OnlineFbank(opts)
        fb.accept_waveform(16000, (audio * 32768.0).tolist())
        fb.input_finished()
        n = fb.num_frames_ready
        if n == 0:
            return np.zeros((1, 80), dtype=np.float32)
        # NO CMN — spike-verified: normalization inverts this model's metric.
        return np.stack([fb.get_frame(i) for i in range(n)]).astype(np.float32)

    @staticmethod
    def _trim(audio: np.ndarray, thresh: float = 0.008) -> np.ndarray:
        """Cut leading/trailing quiet. The segmenter pads utterances with up
        to silence_ms of dead air (whisper likes it; the embedder does NOT):
        untrimmed human clips scored BELOW clean TTS impostor clips at
        enrollment and the lock rejected its own owner (live-hit)."""
        energy = np.abs(audio)
        idx = np.where(energy > thresh)[0]
        if len(idx) == 0:
            return audio
        return audio[idx[0]: idx[-1] + 1]

    def embed(self, audio: np.ndarray) -> np.ndarray:
        audio = self._trim(np.asarray(audio, dtype=np.float32))
        feats = self._fbank(audio)[None, ...]
        emb = self._session().run(None, {"feats": feats})[0][0]
        return emb / (np.linalg.norm(emb) + 1e-9)

    # ---- enrollment ------------------------------------------------------

    def enroll(
        self,
        clips: list[np.ndarray],
        impostor_clips: list[np.ndarray] | None = None,
    ) -> dict:
        """Build + persist the voiceprint. `impostor_clips` (the agent's own
        TTS voice, synthesized at enrollment) calibrate the threshold: the
        bar lands between your self-similarity and the closest impostor."""
        embs = [self.embed(c) for c in clips]
        # Outlier rejection: ONE broken capture (clipped mic, a cough, a
        # rushed take) must not poison the whole print — live-hit: six clips
        # at 0.70-0.90 and one at 0.28 dragged calibration into degraded
        # mode. A clip whose leave-one-out sim is far below the others gets
        # dropped (never below 3 clips kept).
        dropped = 0
        while len(embs) > 3:
            sims = _loo_sims(embs)
            worst = int(np.argmin(sims))
            others = [s for i, s in enumerate(sims) if i != worst]
            if sims[worst] < 0.5 and (np.mean(others) - sims[worst]) > 0.25:
                embs.pop(worst)
                dropped += 1
            else:
                break
        vp = np.mean(embs, axis=0)
        vp /= np.linalg.norm(vp) + 1e-9
        # LEAVE-ONE-OUT self-similarity: each clip scored against the mean of
        # the OTHERS. Scoring against a mean it belongs to inflated self-sims
        # to ~0.97 and calibrated the threshold above real future probes
        # (live-hit: the enrolled voice got refused).
        self_sims = _loo_sims(embs)
        impostor_sims = [
            float(np.dot(vp, self.embed(c))) for c in (impostor_clips or [])
        ]
        threshold = DEFAULT_THRESHOLD
        degraded = False
        if impostor_sims:
            lo, hi = max(impostor_sims), min(self_sims)
            if hi <= lo:
                # Measurement failed to separate owner from impostor. The
                # ONE forbidden outcome is locking the owner out (live-hit:
                # threshold landed above every self-sim and the session went
                # deaf). Favor the owner: bar just under their weakest clip;
                # the lock stays useful against clearly different voices.
                threshold = hi - 0.02
                degraded = True
            else:
                # 35% up from the worst impostor, NOT the midpoint: future
                # accepts say NEW sentences (content variance pulls same-
                # voice scores well below enrollment self-similarity), so
                # the accept side needs most of the headroom.
                threshold = max(lo + 0.35 * (hi - lo), lo + 0.03)
                threshold = min(threshold, hi - 0.02)  # owner always passes
        threshold = float(np.clip(threshold, THRESHOLD_FLOOR, THRESHOLD_CEIL))
        self.voiceprint, self.threshold = vp, threshold
        self.meta = {
            "self_sims": [round(s, 3) for s in self_sims],
            "impostor_sims": [round(s, 3) for s in impostor_sims],
            "threshold": round(threshold, 3),
            "degraded": degraded,
            "dropped_clips": dropped,
            "margin": round(
                min(self_sims) - max(impostor_sims), 3
            ) if impostor_sims else None,
        }
        path = voiceprint_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez(
            path, voiceprint=vp, threshold=threshold,
            meta=json.dumps(self.meta),
        )
        return self.meta

    def load(self) -> bool:
        try:
            data = np.load(voiceprint_path(), allow_pickle=False)
        except (FileNotFoundError, OSError, ValueError):
            return False
        self.voiceprint = data["voiceprint"]
        self.threshold = float(data["threshold"])
        try:
            self.meta = json.loads(str(data["meta"]))
        except Exception:
            self.meta = {}
        return True

    # ---- verification ----------------------------------------------------

    def verify(self, audio: np.ndarray, sample_rate: int = 16000) -> tuple[bool, float]:
        """(is_you, cosine). Short clips pass unlocked — wake words must
        never be eaten by an unreliable short-utterance embedding."""
        if self.voiceprint is None:
            return True, 1.0
        if len(audio) < MIN_VERIFY_S * sample_rate:
            return True, 1.0
        score = float(np.dot(self.voiceprint, self.embed(audio)))
        return score >= self.threshold, score


def _loo_sims(embs: list[np.ndarray]) -> list[float]:
    """Leave-one-out similarity of each embedding vs the mean of the rest."""
    sims = []
    for i, e in enumerate(embs):
        others = [x for j, x in enumerate(embs) if j != i]
        if not others:
            sims.append(1.0)
            continue
        loo = np.mean(others, axis=0)
        loo = loo / (np.linalg.norm(loo) + 1e-9)
        sims.append(float(np.dot(loo, e)))
    return sims


def enrolled() -> bool:
    return os.path.exists(voiceprint_path())


def _tts_impostor_clips(voice: str | None) -> list[np.ndarray]:
    """Synthesize the agent's OWN voice reading two sentences — the impostor
    the threshold most needs to reject. macOS `say` only; empty elsewhere."""
    import subprocess
    import tempfile
    import wave

    clips: list[np.ndarray] = []
    texts = ENROLL_SENTENCES[:2]
    for text in texts:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                aiff = os.path.join(tmp, "p.aiff")
                wav = os.path.join(tmp, "p.wav")
                cmd = ["say"]
                if voice:
                    cmd += ["-v", voice]
                subprocess.run(cmd + ["-o", aiff, text], check=True, timeout=30)
                subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                     aiff, wav], check=True, timeout=30,
                )
                with wave.open(wav) as w:
                    pcm = np.frombuffer(
                        w.readframes(w.getnframes()), dtype=np.int16
                    )
                clips.append(pcm.astype(np.float32) / 32768.0)
        except Exception:
            continue
    return clips


async def run_enrollment(cfg) -> bool:
    """Guided enrollment: read three sentences into the live mic, calibrate
    against the agent's own TTS voice, save the voiceprint. Returns True on
    success. Speaks nothing — enrollment happens before the voice stack."""
    import asyncio

    from . import factory
    from .audio import Mic, resolve_device
    from .segment import segment_utterances
    from .speechcheck import build_speech_check

    print()
    print("🔒 voice-lock enrollment — a guided minute. Each step tells you "
          "HOW to say it; the variety is what makes the lock trust you at "
          "any volume.")
    ensure_model()
    lock = VoiceLock()
    await asyncio.to_thread(lock.warmup)

    try:
        input_idx = resolve_device(cfg.input_device, "input")
    except ValueError as exc:
        print(f"[device] {exc}")
        return False
    mic = Mic(cfg.sample_rate, factory.frame_samples(cfg), device=input_idx)
    vad = factory.build_vad(cfg)
    gate = build_speech_check(True)  # Silero only — no lock during enrollment
    mic.start()
    clips: list[np.ndarray] = []
    try:
        seg = segment_utterances(mic.frames(), vad, cfg)
        total = len(ENROLL_STEPS)
        for i, (style, sentence) in enumerate(ENROLL_STEPS, 1):
            print(f"\n  {i}/{total} — {style}:\n     “{sentence}”")
            while True:
                utterance = await asyncio.wait_for(anext(seg), timeout=120)
                if len(utterance) < 1.2 * cfg.sample_rate:
                    print("     (too short — once more, the whole sentence)")
                    continue
                if gate is not None and not await asyncio.to_thread(
                    gate, utterance, cfg.sample_rate
                ):
                    print("     (that didn't sound like speech — try again)")
                    continue
                clips.append(utterance)
                print("     ✓ got it")
                break
    except (asyncio.TimeoutError, StopAsyncIteration):
        print("  (no speech heard — enrollment cancelled)")
        return False
    finally:
        mic.stop()

    print("\n  calibrating against the agent's own voice…")
    impostors = await asyncio.to_thread(_tts_impostor_clips, cfg.voice)
    meta = lock.enroll(clips, impostors)
    print(f"  saved → {voiceprint_path()}")
    print(f"  self-similarity {meta['self_sims']} · "
          f"TTS-voice {meta['impostor_sims'] or 'n/a'} · "
          f"threshold {meta['threshold']}")
    if meta.get("degraded"):
        print("  ⚠ couldn't cleanly separate your voice from the agent's — "
              "the lock is set PERMISSIVE (you always pass; very different "
              "voices are still refused). Re-enroll with headphones or a "
              "quieter room for a stronger lock.")
    elif meta.get("margin") is not None and meta["margin"] < 0.08:
        print("  ⚠ thin margin — voice-lock may misfire; re-enroll in a "
              "quieter room, or run team sessions until v-next tuning")
    print("  say 'team session' any time to let everyone talk; "
          "'solo session' locks back to you.")
    return True

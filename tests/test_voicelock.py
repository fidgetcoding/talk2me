"""Voice-lock suite, two tiers.

Tier 1 (always runs, no model): threshold calibration math, save/load
round-trip, short-utterance pass-through, and the gate's solo/team switch —
all with a stubbed embedder.

Tier 2 (self-skips without the ONNX model + macOS `say`): the real thing —
enroll the Ava TTS voice as "the user", verify that Ava passes and Karen is
refused with the calibrated threshold. This is the discrimination proof; CI
(no model download) skips it honestly.

Run:  ./.venv/bin/python -m tests.test_voicelock
"""

import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

RESULTS: list[bool] = []


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    RESULTS.append(ok)
    return ok


def test_logic_with_stub_embedder() -> None:
    from talk2me.voicelock import THRESHOLD_CEIL, THRESHOLD_FLOOR, VoiceLock

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_VOICEPRINT"] = os.path.join(tmp, "vp.npz")
        lock = VoiceLock(path="/nonexistent-model")
        # Stub embedder: "voice identity" = normalized mean sign pattern.
        lock.embed = lambda a: (  # type: ignore[method-assign]
            np.array([1.0, 0.0]) if float(np.mean(a)) >= 0 else np.array([0.0, 1.0])
        )
        me = [np.ones(16000, dtype=np.float32) * v for v in (0.5, 0.6, 0.7)]
        impostor = [np.ones(16000, dtype=np.float32) * -0.5]
        meta = lock.enroll(me, impostor)
        _report(
            "calibrated threshold lands between self and impostor",
            THRESHOLD_FLOOR <= meta["threshold"] <= THRESHOLD_CEIL
            and meta["self_sims"][0] > meta["impostor_sims"][0],
        )

        ok_me, s_me = lock.verify(me[0])
        ok_imp, s_imp = lock.verify(impostor[0])
        _report(
            "verify accepts self, refuses impostor",
            ok_me is True and ok_imp is False and s_me > s_imp,
        )
        _report(
            "short utterance passes unlocked (wake words never eaten)",
            lock.verify(np.ones(4000, dtype=np.float32) * -0.5)[0] is True,
        )

        # Round-trip through disk.
        lock2 = VoiceLock(path="/nonexistent-model")
        _report("voiceprint load round-trip", lock2.load() is True
                and abs(lock2.threshold - meta["threshold"]) < 1e-6)

        # THE forbidden outcome: an impostor scoring above the owner must
        # never produce a threshold that locks the owner out (live-hit:
        # Nate's real voice scored below the TTS impostor and the session
        # went deaf).
        lock3 = VoiceLock(path="/nonexistent-model")
        embs = iter([
            # Owner clips that DISAGREE with each other (mutual sim 0.6)…
            np.array([1.0, 0.0]), np.array([0.6, 0.8]),
            # …while the impostor sits right on their mean: impostor sim
            # (~1.0) > min self sim (0.6) — the inverted measurement.
            np.array([0.894, 0.447]),
        ])
        lock3.embed = lambda a: (n := next(embs)) / np.linalg.norm(n)  # type: ignore[method-assign]
        meta3 = lock3.enroll(
            [np.ones(16000, dtype=np.float32)] * 2,
            [np.ones(16000, dtype=np.float32)],
        )
        _report(
            "inverted calibration never locks the owner out",
            meta3["degraded"] is True
            and meta3["threshold"] < min(meta3["self_sims"]),
        )
    os.environ["TALK2ME_VOICEPRINT"] = "/nonexistent-t2m-test-voiceprint"


def test_gate_solo_team_switch() -> None:
    from talk2me.speechcheck import SileroSpeechCheck

    class FakeLock:
        def __init__(self) -> None:
            self.calls = 0

        def verify(self, audio, sample_rate=16000):
            self.calls += 1
            return False, 0.1  # always "not you"

    fake = FakeLock()
    gate = SileroSpeechCheck(voicelock=fake)
    _report("gate starts locked when a voicelock is attached", gate.locked is True)
    gate.set_locked(False)
    _report("team session unlocks", gate.locked is False)
    gate.set_locked(True)
    _report("solo session re-locks", gate.locked is True)
    bare = SileroSpeechCheck(voicelock=None)
    bare.set_locked(True)
    _report("no voicelock -> can never lock", bare.locked is False)


def _say(voice: str, text: str, tmp: str, name: str) -> np.ndarray | None:
    try:
        aiff = os.path.join(tmp, name + ".aiff")
        wav = os.path.join(tmp, name + ".wav")
        subprocess.run(["say", "-v", voice, "-o", aiff, text],
                       check=True, timeout=60, capture_output=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c",
                        "1", aiff, wav], check=True, timeout=60,
                       capture_output=True)
        with wave.open(wav) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0
    except Exception:
        return None


def test_real_model_discrimination() -> None:
    from talk2me.voicelock import ENROLL_SENTENCES, VoiceLock, model_path

    if not os.path.exists(model_path()) or sys.platform != "darwin":
        print("[SKIP] real-model discrimination (model or `say` unavailable)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_VOICEPRINT"] = os.path.join(tmp, "vp.npz")
        me = [_say("Ava (Premium)", t, tmp, f"me{i}")
              for i, t in enumerate(ENROLL_SENTENCES[:3])]
        other = _say("Karen (Premium)",
                     "Wake up and continue the previous session now.", tmp, "o1")
        probe = _say("Ava (Premium)",
                     "Please add the melee attack to the game for me.", tmp, "p1")
        if any(c is None for c in [*me, other, probe]):
            print("[SKIP] real-model discrimination (voice synth failed)")
            return
        lock = VoiceLock()
        meta = lock.enroll(me, [other])
        ok_me, s_me = lock.verify(probe)
        ok_other, s_other = lock.verify(other)
        _report(
            f"REAL: enrolled voice passes ({s_me:+.2f}), other voice refused "
            f"({s_other:+.2f}), threshold {meta['threshold']}",
            ok_me is True and ok_other is False,
        )
    os.environ["TALK2ME_VOICEPRINT"] = "/nonexistent-t2m-test-voiceprint"


def main() -> int:
    os.environ.setdefault("TALK2ME_VOICEPRINT", "/nonexistent-t2m-test-voiceprint")
    test_logic_with_stub_embedder()
    test_gate_solo_team_switch()
    test_real_model_discrimination()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall "
          f"({sum(RESULTS)}/{len(RESULTS)} groups passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

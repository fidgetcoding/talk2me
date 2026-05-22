"""Contract test for the WebRTC VAD adapter. No mic, no model.

Asserts the float32->PCM16 wiring, the exact-frame guard (a partial trailing
frame must degrade to non-speech, never crash the C call), and the up-front
validation of sample rate / frame duration / aggressiveness.

Run:  ./.venv/bin/python -m tests.test_webrtc_vad
Requires:  pip install talk2me[webrtc]
"""

import numpy as np

from talk2me.vad.webrtc import WebrtcVAD

SR = 16000
FRAME = 480  # 30 ms @ 16 kHz — a valid WebRTC frame


def main() -> int:
    results: list[tuple[str, bool]] = []

    vad = WebrtcVAD(sample_rate=SR, frame_samples=FRAME, aggressiveness=2)

    # Silence is reliably non-speech across all aggressiveness levels.
    silence = np.zeros(FRAME, dtype=np.float32)
    results.append(("silence -> not speech", vad.is_speech(silence) is False))

    # A short/long frame (stream-end partial) must be treated as non-speech, not
    # passed to the C call where a wrong length raises.
    results.append(("partial frame -> not speech", vad.is_speech(np.zeros(123, dtype=np.float32)) is False))
    results.append(("oversized frame -> not speech", vad.is_speech(np.zeros(FRAME * 2, dtype=np.float32)) is False))

    # is_speech returns a real bool (PCM conversion path runs without raising) for
    # a full-length loud frame, whatever the GMM verdict.
    loud = (0.3 * np.sin(2 * np.pi * 220 * np.arange(FRAME) / SR)).astype(np.float32)
    results.append(("full loud frame -> bool", isinstance(vad.is_speech(loud), bool)))

    # Validation: bad sample rate, bad frame duration, bad aggressiveness.
    for label, fn in [
        ("bad sample_rate raises", lambda: WebrtcVAD(sample_rate=44100, frame_samples=FRAME)),
        ("bad frame_ms raises", lambda: WebrtcVAD(sample_rate=SR, frame_samples=512)),
        ("bad aggressiveness raises", lambda: WebrtcVAD(sample_rate=SR, frame_samples=FRAME, aggressiveness=7)),
    ]:
        try:
            fn()
        except ValueError:
            results.append((label, True))
        except Exception:
            results.append((label, False))
        else:
            results.append((label, False))

    for label, ok in results:
        print(f"[{label}] -> {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"-> {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

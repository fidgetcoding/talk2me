"""Device-resolution test. No audio hardware required — resolve_device is pure
spec->index mapping, and the enumeration/format helpers degrade gracefully when
portaudio has no devices (or isn't present at all).

Run:  ./.venv/bin/python -m tests.test_devices
"""

from types import SimpleNamespace
from unittest import mock

from talk2me import audio
from talk2me.audio import (
    _name_looks_like_speakers,
    devices_of_kind,
    format_device_table,
    resolve_device,
)


def _table_with_fake_devices() -> str:
    fake_sd = SimpleNamespace(
        default=SimpleNamespace(device=(0, 1)),
        query_devices=lambda: [
            {"name": "Fake Mic", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "Fake Phones", "max_input_channels": 0, "max_output_channels": 2},
        ],
    )
    with mock.patch.object(audio, "sd", fake_sd):
        return format_device_table()


def main() -> int:
    results: list[tuple[str, bool]] = []

    # None -> system default (None). Int / digit-string -> that index verbatim,
    # without touching hardware.
    results.append(("None -> None", resolve_device(None, "input") is None))
    results.append(("int -> index", resolve_device(7, "output") == 7))
    results.append(("digit str -> index", resolve_device("7", "output") == 7))
    results.append(("negative digit str -> index", resolve_device("-1", "input") == -1))

    # A name that can't match anything (or no devices present) -> clean ValueError,
    # never a silent wrong-device capture.
    try:
        resolve_device("zzz-no-such-device-zzz", "input")
    except ValueError:
        results.append(("unknown name -> ValueError", True))
    else:
        results.append(("unknown name -> ValueError", False))

    # Enumeration + table never crash, regardless of hardware state.
    in_devs = devices_of_kind("input")
    out_devs = devices_of_kind("output")
    results.append(("devices_of_kind returns list", isinstance(in_devs, list) and isinstance(out_devs, list)))
    results.append((
        "tuples are (int, str)",
        all(isinstance(i, int) and isinstance(n, str) for i, n in in_devs + out_devs),
    ))
    table = format_device_table()
    if audio.sd is None:
        results.append(("table says audio is unavailable", "unavailable" in table))
    else:
        results.append(("table mentions INPUT/OUTPUT", "INPUT" in table and "OUTPUT" in table))

    # Table layout, on any machine: each device under its heading, defaults starred.
    fake = _table_with_fake_devices()
    results.append((
        "table lists devices under their headings",
        fake.index("INPUT") < fake.index("[0] Fake Mic")
        < fake.index("OUTPUT") < fake.index("[1] Fake Phones"),
    ))
    results.append((
        "table stars the defaults",
        "  * [0] Fake Mic" in fake and "  * [1] Fake Phones" in fake,
    ))

    # Speaker classifier: confident only on speaker-named devices — BT/jack
    # devices have arbitrary names and must NOT classify as speakers.
    speaker_cases = [
        ("MacBook Pro Speakers", True),
        ("Built-in Output", True),
        ("JBL Flip Speaker", True),
        ("Megapods", False),
        ("External Headphones", False),
        ("AirPods Max", False),
    ]
    results.append((
        "speaker-name classifier",
        all(_name_looks_like_speakers(n) == want for n, want in speaker_cases),
    ))

    for label, ok in results:
        print(f"[{label}] -> {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"-> {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

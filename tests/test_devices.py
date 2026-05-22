"""Device-resolution test. No audio hardware required — resolve_device is pure
spec->index mapping, and the enumeration/format helpers degrade gracefully when
portaudio has no devices (or isn't present at all).

Run:  ./.venv/bin/python -m tests.test_devices
"""

from talk2me.audio import devices_of_kind, format_device_table, resolve_device


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
    results.append(("table mentions INPUT/OUTPUT", "INPUT" in table and "OUTPUT" in table))

    for label, ok in results:
        print(f"[{label}] -> {'PASS' if ok else 'FAIL'}")
    all_ok = all(ok for _, ok in results)
    print(f"-> {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

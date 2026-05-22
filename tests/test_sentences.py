"""Pure-logic test for the streaming sentence drainer. Instant, no audio.

Exercises talk2me.orchestrator._drain_sentences — the function that pulls
complete sentences out of a streaming text buffer so the orchestrator can
speak them one at a time as assistant deltas arrive.

Covers: full multi-sentence flush, incremental delta feeding, partial
retention, decimal-point safety, and the DESIRED post-abbreviation-fix
behavior (e.g. / i.e. should not split a sentence). The abbreviation case
is labeled "expected-after-abbrev-fix" — another agent owns the fix in
orchestrator.py, so if it isn't in yet this case fails loudly but the
script still exits non-zero so the gap stays visible.

Run:  ./.venv/bin/python -m tests.test_sentences
"""

from talk2me.orchestrator import _drain_sentences


def main() -> int:
    results: list[tuple[str, bool, str]] = []  # (label, passed, detail)

    # (a) Two complete sentences in one buffer -> both drained, empty remainder.
    rem, sents = _drain_sentences("Hello world. How are you?")
    a_ok = sents == ["Hello world.", "How are you?"] and rem == ""
    results.append(("a/two-sentences", a_ok, f"rem={rem!r} sents={sents!r}"))

    # (b) Incremental feeding: simulate assistant text deltas. Concatenate the
    #     leftover remainder with each new delta, exactly as the orchestrator does.
    #     Deltas concatenate verbatim (no synthetic spaces) — the boundary space
    #     here lives at the end of the first delta, mirroring real token streams.
    buf = ""
    drained: list[str] = []
    for delta in ["Two plus ", "two is four."]:
        buf += delta
        buf, ready = _drain_sentences(buf)
        drained.extend(ready)
    b_ok = drained == ["Two plus two is four."] and buf == ""
    results.append(("b/incremental", b_ok, f"buf={buf!r} drained={drained!r}"))

    # (c) Partial text with no terminator stays entirely in the remainder.
    rem, sents = _drain_sentences("an unfinished thought with no end")
    c_ok = sents == [] and rem == "an unfinished thought with no end"
    results.append(("c/partial-no-terminator", c_ok, f"rem={rem!r} sents={sents!r}"))

    # (d) Decimals must not split at the inner period: "3.14" stays intact.
    rem, sents = _drain_sentences("Pi is 3.14 exactly.")
    d_ok = sents == ["Pi is 3.14 exactly."] and rem == ""
    results.append(("d/decimal-no-split", d_ok, f"rem={rem!r} sents={sents!r}"))

    # (e) DESIRED post-abbreviation-fix behavior: "e.g." must not end a sentence.
    rem, sents = _drain_sentences("Use e.g. this one. Done.")
    e_ok = sents == ["Use e.g. this one.", "Done."] and rem == ""
    results.append(("e/abbrev (expected-after-abbrev-fix)", e_ok,
                    f"rem={rem!r} sents={sents!r}"))

    for label, passed, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}  {detail}")

    failed = [label for label, passed, _ in results if not passed]
    all_ok = not failed
    if not all_ok:
        print(f"\nFAILING: {', '.join(failed)}")
        # The abbrev case is the only one expected to fail before the fix lands.
        if failed == ["e/abbrev (expected-after-abbrev-fix)"]:
            print("note: only the expected-after-abbrev-fix case failed — "
                  "current _drain_sentences splits at 'e.g.'; awaiting the "
                  "abbreviation fix in orchestrator.py.")
    print(f"-> {'PASS' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

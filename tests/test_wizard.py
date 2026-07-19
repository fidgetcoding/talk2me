"""Setup-wizard plumbing, non-interactively. The prompts themselves are not
driven here (they need a TTY); what's locked is everything around them:
config save/load round-trip, corrupt-file tolerance, defaults injection into
argument parsing, and the first-run trigger staying quiet for pipes, power
users, and existing configs.

Run:  ./.venv/bin/python -m tests.test_wizard
"""

import json
import os
import tempfile

RESULTS: list[bool] = []


def _report(group: str, ok: bool) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {group}")
    RESULTS.append(ok)
    return ok


def test_save_load_roundtrip() -> None:
    from talk2me.wizard import load_saved_config, save_config

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_CONFIG"] = os.path.join(tmp, "cfg.json")
        cfg = {
            "model": "opus", "stt": "parakeet", "voice": "Ava (Premium)",
            "barge_in": True, "gated": False, "cwd": "/tmp",
            "save_dir": None,
        }
        save_config(cfg)
        loaded = load_saved_config()
        _report("save/load round-trip", loaded == cfg)

        # Unknown keys in a hand-edited file are ignored, known ones kept.
        with open(os.environ["TALK2ME_CONFIG"], "w") as fh:
            json.dump({"model": "haiku", "hacker": "yes"}, fh)
        _report(
            "unknown keys ignored",
            load_saved_config() == {"model": "haiku"},
        )

        # Corrupt file -> empty config, never a crash.
        with open(os.environ["TALK2ME_CONFIG"], "w") as fh:
            fh.write("{nope")
        _report("corrupt file tolerated", load_saved_config() == {})
    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"


def test_saved_config_becomes_defaults() -> None:
    from talk2me.__main__ import _parse_args
    from talk2me.wizard import save_config

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_CONFIG"] = os.path.join(tmp, "cfg.json")
        save_config({
            "model": "opus", "stt": "parakeet", "voice": "Ava (Premium)",
            "barge_in": False, "gated": True, "cwd": tmp, "save_dir": None,
        })
        cfg = _parse_args([])
        _report(
            "file values become defaults",
            cfg.model == "opus" and cfg.stt == "parakeet"
            and cfg.voice == "Ava (Premium)" and cfg.barge_in is False
            and cfg.half_duplex is True and cfg.cwd == tmp
            and "bypass" not in cfg.permission_mode,  # gated=True
        )
        # Explicit flags beat the file.
        cfg2 = _parse_args(["--model", "haiku", "--barge-in"])
        _report(
            "flags beat the file",
            cfg2.model == "haiku" and cfg2.barge_in is True,
        )
    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"


def test_first_run_trigger() -> None:
    from talk2me.wizard import should_run_first_time

    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"
    # This test process runs with piped stdio -> never first-run here.
    _report("non-TTY never triggers", should_run_first_time([]) is False)
    _report(
        "identity flags suppress",
        should_run_first_time(["--model", "opus"]) is False,
    )


def test_session_continuity() -> None:
    from talk2me.backends.claude_code import ClaudeCodeBackend
    from talk2me.continuity import load_last_session, save_last_session

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_STATE"] = os.path.join(tmp, "state.json")
        _report("no state -> None", load_last_session(tmp) is None)
        save_last_session(tmp, "sess-abc")
        save_last_session("/somewhere-else-nonexistent", "sess-zzz")
        _report(
            "state keyed by working dir",
            load_last_session(tmp) == "sess-abc",
        )

        # --continue resolves the stored id into the parsed config…
        from talk2me.__main__ import _parse_args

        cfg = _parse_args(["--continue", "--cwd", tmp])
        _report("--continue resolves session id", cfg.resume_session_id == "sess-abc")
        cfg2 = _parse_args(["--cwd", tmp])
        _report("fresh by default", cfg2.resume_session_id is None)

        # …and the backend swaps --session-id for --resume.
        argv = ClaudeCodeBackend(resume_session_id="sess-abc")._argv()
        fresh_argv = ClaudeCodeBackend()._argv()
        _report(
            "backend argv uses --resume",
            "--resume" in argv
            and argv[argv.index("--resume") + 1] == "sess-abc"
            and "--session-id" not in argv
            and "--session-id" in fresh_argv,
        )
    os.environ["TALK2ME_STATE"] = "/nonexistent-t2m-test-state"


def main() -> int:
    os.environ.setdefault("TALK2ME_CONFIG", "/nonexistent-t2m-test-config")
    test_save_load_roundtrip()
    test_saved_config_becomes_defaults()
    test_first_run_trigger()
    test_session_continuity()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall "
          f"({sum(RESULTS)}/{len(RESULTS)} groups passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

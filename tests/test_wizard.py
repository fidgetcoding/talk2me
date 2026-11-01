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


def test_saved_posture_is_respected() -> None:
    """The v2.6 gate default must not overrule what anyone already picked."""
    from talk2me.__main__ import _parse_args
    from talk2me.wizard import save_config

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["TALK2ME_CONFIG"] = os.path.join(tmp, "cfg.json")
        # A pre-2.6 wizard run: both keys were always written.
        save_config({"stt": "whisper", "gated": False, "whisper_model": None})
        cfg = _parse_args([])
        _report(
            "saved auto-approve + whisper kept",
            cfg.permission_mode == "bypassPermissions" and cfg.stt == "whisper",
        )
        _report(
            "--gated beats a saved auto-approve",
            _parse_args(["--gated"]).permission_mode == "default",
        )
        save_config({"gated": True})
        _report(
            "--auto-approve beats a saved gate",
            _parse_args(["--auto-approve"]).permission_mode == "bypassPermissions",
        )
        save_config({"stt": "parakeet"})
        _report("saved parakeet kept", _parse_args([]).stt == "parakeet")
        # Enrolling voice-lock writes a file with no wizard answers in it, so
        # the user never picked a posture: they get the safe default.
        save_config({"voice_lock": True})
        cfg = _parse_args([])
        _report(
            "no saved answer -> gated",
            cfg.permission_mode == "default" and cfg.voice_approval is True,
        )
    os.environ["TALK2ME_CONFIG"] = "/nonexistent-t2m-test-config"


def test_wizard_defaults() -> None:
    """Enter through every prompt: what a new user gets, and what a returning
    one keeps. The prompts are stubbed to take their defaults; no TTY needed."""
    from rich.prompt import Confirm, Prompt

    from talk2me import wizard

    real = Prompt.ask, Confirm.ask, wizard._installed_voices, wizard.whisper_gpu_supported, wizard._offer_install
    asked: dict[str, list[str]] = {}

    def take_default(prompt, *, default=None, choices=None, **_kw):
        asked[prompt.strip()] = choices or []
        return default

    Prompt.ask = staticmethod(take_default)
    Confirm.ask = staticmethod(lambda _prompt, *, default=None, **_kw: default)
    wizard._installed_voices = list
    # Never let a test reach the real pip.
    wizard._offer_install = lambda _p, _name, _module, _pip: True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TALK2ME_CONFIG"] = os.path.join(tmp, "cfg.json")
            wizard.whisper_gpu_supported = lambda: True
            fresh = wizard.run_wizard({})
            apple_choices = asked["ears"]
            kept = wizard.run_wizard({"stt": "whisper", "gated": False})
            kept_gpu = wizard.run_wizard({"stt": "whisper-gpu"})
            wizard.whisper_gpu_supported = lambda: False
            linux = wizard.run_wizard({"stt": "whisper-gpu"})
            linux_choices = asked["ears"]
    finally:
        Prompt.ask, Confirm.ask, wizard._installed_voices, wizard.whisper_gpu_supported, wizard._offer_install = real
    _report("wizard default posture is gated", fresh["gated"] is True)
    _report(
        "wizard defaults to base.en, lists whisper-gpu as an option",
        fresh["stt"] == "whisper" and "whisper-gpu" in apple_choices,
    )
    _report("wizard keeps a saved whisper-gpu", kept_gpu["stt"] == "whisper-gpu")
    _report("wizard keeps a saved whisper + auto-approve",
            kept["stt"] == "whisper" and kept["gated"] is False)
    _report("wizard hides whisper-gpu elsewhere",
            "whisper-gpu" not in linux_choices and linux["stt"] == "whisper")
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
    # CI has no `claude` on PATH, and these tests don't launch one.
    from talk2me import factory, preflight

    preflight.agent_problem = lambda _agent: None
    # These check that a saved engine is kept, on machines with or without a GPU.
    factory.whisper_gpu_supported = lambda: True
    factory.parakeet_installed = lambda: True
    test_save_load_roundtrip()
    test_saved_config_becomes_defaults()
    test_saved_posture_is_respected()
    test_wizard_defaults()
    test_first_run_trigger()
    test_session_continuity()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall "
          f"({sum(RESULTS)}/{len(RESULTS)} groups passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

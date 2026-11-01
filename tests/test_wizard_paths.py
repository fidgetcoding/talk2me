"""The setup wizard as a person meets it: a real process, real prompts, scripted
answers on stdin, down every path. CI runs it on Linux, macOS and Windows. A wizard
run never launches a session here, so no audio and no agent.

Run:  python -m tests.test_wizard_paths
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from talk2me.factory import whisper_gpu_supported
from talk2me.sandbox import available as sandbox_available

RESULTS: list[bool] = []
RUN = "from talk2me.wizard import run_wizard; import json; print('RESULT' + json.dumps(run_wizard({})))"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""), flush=True)
    RESULTS.append(bool(ok))


def _wizard(*, provider=("claude", ""), ears="", language="", tools="", folder="", lock="n", save=("n",), extra_env=None) -> tuple[dict | None, str, int]:
    """Answers in prompt order. Which prompts appear depends on the platform and
    answers, same as for a person, so this mirrors those rules."""
    lines = [*provider, ears]
    installed = ears == "parakeet" and importlib.util.find_spec("parakeet_mlx") is not None
    if ears == "parakeet" and not installed:
        lines.append("n")  # decline the pip offer
    if not installed:
        lines.append(language)
    if sys.platform == "darwin":
        lines.append("system")
    lines += ["", tools]
    if "codex" not in provider and sandbox_available() is None:
        lines.append("n")
    lines += [folder, lock, *save]
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp, "config.json")
        env = {**os.environ, "TALK2ME_CONFIG": str(config), "HOME": tmp, "USERPROFILE": tmp, "PYTHONUTF8": "1",
               "PATH": os.environ["PATH"], **(extra_env or {})}
        proc = subprocess.run(
            [sys.executable, "-c", RUN], input="\n".join(lines) + "\n", capture_output=True, text=True,
            encoding="utf-8", env=env, timeout=120, check=False,
        )
        result = None
        for line in proc.stdout.splitlines():
            if "RESULT{" in line:
                result = json.loads(line[line.index("RESULT{") + 6 :])
        if config.exists() and result is not None:
            saved = json.loads(config.read_text())
            result["_saved_matches"] = all(saved.get(k) == result[k] for k in saved)
        return result, proc.stdout + proc.stderr, proc.returncode


def test_defaults() -> None:
    result, out, code = _wizard()
    check("Enter through everything works", code == 0 and result is not None, out[-200:])
    if not result:
        return
    check("defaults: whisper, gated, no lock", result["stt"] == "whisper" and result["gated"] is True and result["voice_lock"] is False)
    check("what's saved is what it said", result["_saved_matches"])
    check("no traceback", "Traceback" not in out)
    if sys.platform != "darwin":
        check("no macOS talk off a Mac", "FaceTime" not in out and "Ava (Premium)" not in out, out[:0])
    if not whisper_gpu_supported():
        check("no GPU option where it can't run", "whisper-gpu" not in out)


def test_providers() -> None:
    result, out, _ = _wizard(provider=("codex", "o4"))
    check("codex", result and result["agent"] == "codex" and result["model"] == "o4", out[-160:])
    check("codex warns when not logged in", "codex login" in out)
    result, out, _ = _wizard(provider=("kimi", ""))
    check("kimi fills in its endpoint", result and "moonshot" in result["backend_base_url"] and result["backend_auth_env"] == "MOONSHOT_API_KEY")
    check("kimi says the key's missing", "MOONSHOT_API_KEY" in out)
    result, _, _ = _wizard(provider=("glm", "glm-x"))
    check("glm takes a model", result and result["model"] == "glm-x")
    result, _, _ = _wizard(provider=("deepseek", ""))
    check("deepseek", result and "deepseek" in result["backend_base_url"])
    result, out, _ = _wizard(provider=("custom", "https://example.test/v1", "MY_KEY", "m1"))
    check("custom endpoint", result and result["backend_base_url"] == "https://example.test/v1" and result["backend_auth_env"] == "MY_KEY", out[-160:])
    check("never writes a key to the file", result and "MY_KEY" == result["backend_auth_env"])


def test_a_bad_answer_reasks() -> None:
    result, out, _ = _wizard(provider=("banana", "claude", ""))
    check("a provider that doesn't exist re-asks", result and result["agent"] == "claude" and "Please select" in out, out[-160:])


def test_ears() -> None:
    result, out, _ = _wizard(ears="whisper", language="es")
    check("Spanish moves off the English-only model", result and result["language"] == "es" and result["whisper_model"] == "base", out[-160:])
    if importlib.util.find_spec("parakeet_mlx") is None:
        result, out, _ = _wizard(ears="parakeet")
        check("declining parakeet's install falls back", result and result["stt"] == "whisper", out[-160:])


def test_tools() -> None:
    result, _, _ = _wizard(tools="auto-approve")
    check("auto-approve is saved", result and result["gated"] is False)


def test_folders() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result, out, _ = _wizard(folder=tmp)
        check("a real folder pins", result and os.path.realpath(result["cwd"]) == os.path.realpath(tmp), out[-160:])
        missing = os.path.join(tmp, "nope")
        result, out, _ = _wizard(folder=f"{missing}\n{tmp}")
        check("a wrong folder re-asks", result and "not a folder" in out and os.path.realpath(result["cwd"]) == os.path.realpath(tmp))
        logs = os.path.join(tmp, "logs")
        result, out, _ = _wizard(save=("y", logs))
        check("transcripts folder gets made", result and result["save_dir"] == logs and os.path.isdir(logs), out[-160:])


def test_cancel_at_a_terminal() -> None:
    """Ctrl-D at the first prompt of `t2m --setup`, through a real terminal."""
    if sys.platform == "win32":
        return
    import pty
    import select

    master, slave = pty.openpty()
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp, "config.json")
        env = {**os.environ, "TALK2ME_CONFIG": str(config), "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}
        claude = Path(tmp, "claude")
        claude.write_text("#!/bin/sh\nexit 0\n")
        claude.chmod(0o755)
        # --text: if cancelling ever failed to stop it, it still couldn't touch audio.
        proc = subprocess.Popen([sys.executable, "-m", "talk2me", "--setup", "--text"], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        out = b""
        sent = False
        while proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 15)
            if not ready:
                proc.kill()
                break
            try:
                out += os.read(master, 4096)
            except OSError:
                break
            if not sent and b"provider" in out:
                os.write(master, b"\x04")
                sent = True
        proc.wait(10)
        os.close(master)
        text = out.decode("utf-8", errors="replace")
        check("Ctrl-D cancels setup cleanly", "setup cancelled" in text and "Traceback" not in text, text[-200:])
        check("and saves nothing", not config.exists())


def main() -> int:
    test_defaults()
    test_providers()
    test_a_bad_answer_reasks()
    test_ears()
    test_tools()
    test_folders()
    test_cancel_at_a_terminal()
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

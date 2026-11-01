"""Things worth saying in one sentence before a session starts, instead of as a
traceback once it has. Each check returns the sentence, or None when all's well.
"""

from __future__ import annotations

import importlib.util
import os
import shutil

INSTALL_HINTS = {
    "claude": "install Claude Code first: npm install -g @anthropic-ai/claude-code",
    "codex": "install the Codex CLI first: npm install -g @openai/codex, then codex login",
}


def agent_problem(agent: str) -> str | None:
    if shutil.which(agent):
        return None
    return f"can't find `{agent}` on your PATH. {INSTALL_HINTS.get(agent, '')}".strip()


def cwd_problem(cwd: str | None) -> str | None:
    if cwd and not os.path.isdir(os.path.expanduser(cwd)):
        return f"--cwd isn't a folder: {cwd} (saved by t2m --setup? rerun it to change the pin)"
    return None


def sandbox_project_problem(project: str, home: str | None = None) -> str | None:
    """The sandbox makes the project writable, so a project of $HOME protects nothing."""
    real = os.path.realpath(os.path.expanduser(project))
    real_home = os.path.realpath(home or os.path.expanduser("~"))
    if real_home == real or real_home.startswith(real.rstrip(os.sep) + os.sep):
        return (
            f"--sandbox lets the agent write anywhere in its folder, and that's {real}, which "
            "would include your shell profile and everything else. cd into a project first."
        )
    return None


def voice_option_problem(vad: str, tts: str) -> str | None:
    if vad == "webrtc" and importlib.util.find_spec("webrtcvad") is None:
        return '--vad webrtc needs its extra: pip install "talk2me[webrtc]"'
    if vad == "silero":
        from .vad.silero import _DEFAULT_MODEL_NAME, _ENV_MODEL_VAR

        beside = os.path.join(os.path.dirname(__file__), "vad", _DEFAULT_MODEL_NAME)
        if not (os.path.exists(os.environ.get(_ENV_MODEL_VAR, "")) or os.path.exists(beside)):
            return (
                f"--vad silero needs the model file: download {_DEFAULT_MODEL_NAME} from "
                f"github.com/snakers4/silero-vad and point {_ENV_MODEL_VAR} at it"
            )
    if tts == "kitten" and importlib.util.find_spec("kittentts") is None:
        return '--tts kitten needs its extra: pip install "talk2me[kitten]"'
    return None


def whisper_model_problem(model: str) -> str | None:
    """A typo here otherwise surfaces as a traceback after the agent has started."""
    if os.path.isdir(os.path.expanduser(model)) or "/" in model:
        return None  # a local folder, or a Hugging Face repo
    from faster_whisper.utils import available_models

    known = available_models()
    if model in known:
        return None
    return f"--whisper-model doesn't know {model!r}. Try one of: {', '.join(known[:8])}"


def match_model_to_language(stt: str, model: str, language: str) -> tuple[str, str, str | None]:
    """(model, language, notice). The .en models only hear English: asked for Spanish
    they quietly transcribe it as English gibberish, so move to the multilingual twin."""
    if language == "en":
        return model, language, None
    if stt == "parakeet":
        return model, "en", "parakeet only hears English, so --language is ignored"
    if stt == "whisper" and model.endswith(".en"):
        twin = model.removesuffix(".en")
        return twin, language, f"{model} only hears English, using {twin} for --language {language}"
    return model, language, None


def no_voice_notice(tts: str) -> str | None:
    if tts == "say" and not shutil.which("say") and not shutil.which("espeak-ng"):
        return (
            "no voice on this machine, so answers stay on screen. Install espeak-ng for "
            'a basic one, or pip install "talk2me[kitten]" and use --tts kitten'
        )
    return None

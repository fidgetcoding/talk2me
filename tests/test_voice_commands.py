"""Voice commands beyond English: the grammar, and the same round trip the English
gate gets, with a fake mic. No audio, no model.

Run:  python -m tests.test_voice_commands
"""

import asyncio

import numpy as np

from talk2me.config import Config
from talk2me.events import AssistantTextDelta, PermissionRequest, TurnComplete
from talk2me.orchestrator import Orchestrator, control_intent, match_intent
from talk2me.vad import EnergyVAD
from talk2me.voicecommands import LANGUAGES, active, fold

from .fakes import FakeBackend, FakeMic, FakeSpeaker, FakeSTT, FakeTTS

RESULTS: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    RESULTS.append(bool(ok))


# (language, what Whisper might write, intent), with real accents and punctuation.
CONTROLS = [
    ("es", "Pausa.", "pause"), ("es", "Deja de escuchar.", "pause"), ("es", "Despierta.", "resume"),
    ("es", "¡Ya volví!", "resume"), ("es", "Sesión de equipo.", "team"), ("es", "Solo yo.", "solo"),
    ("es", "Sesión anterior, por favor.", "sessions"), ("es", "Bueno, pausa.", "pause"),
    ("fr", "Pause.", "pause"), ("fr", "Arrête d'écouter.", "pause"), ("fr", "Réveille-toi.", "resume"),
    ("fr", "Je suis là.", "resume"), ("fr", "Session d'équipe.", "team"), ("fr", "Juste moi.", "solo"),
    ("fr", "Session précédente, s'il te plaît.", "sessions"),
    ("de", "Pause.", "pause"), ("de", "Hör auf zuzuhören.", "pause"), ("de", "Wach auf.", "resume"),
    ("de", "Ich bin zurück.", "resume"), ("de", "Team-Sitzung.", "team"), ("de", "Nur ich.", "solo"),
    ("de", "Vorherige Sitzung, bitte.", "sessions"),
    ("it", "Pausa.", "pause"), ("it", "Smetti di ascoltare.", "pause"), ("it", "Svegliati.", "resume"),
    ("it", "Sono tornato.", "resume"), ("it", "Sessione di gruppo.", "team"), ("it", "Solo io.", "solo"),
    ("pt", "Pausa.", "pause"), ("pt", "Faça uma pausa.", "pause"), ("pt", "Acorda.", "resume"),
    ("pt", "Estou de volta.", "resume"), ("pt", "Sessão em equipe.", "team"), ("pt", "Só eu.", "solo"),
    ("pt", "Sessão anterior.", "sessions"),
]
VERDICTS = [
    ("es", "Sí.", "approve"), ("es", "Adelante.", "approve"), ("es", "No.", "deny"), ("es", "Rechaza.", "deny"),
    ("es", "No, no lo hagas.", "deny"), ("es", "Sí, no.", "deny"),
    ("fr", "Oui.", "approve"), ("fr", "D'accord.", "approve"), ("fr", "Vas-y.", "approve"), ("fr", "Non.", "deny"),
    ("fr", "Refuse.", "deny"), ("fr", "Non, ne fais pas ça.", "deny"),
    ("de", "Ja.", "approve"), ("de", "Genehmigen.", "approve"), ("de", "Mach es.", "approve"), ("de", "Nein.", "deny"),
    ("de", "Ablehnen.", "deny"), ("de", "Nein, bitte nicht.", "deny"),
    ("it", "Sì.", "approve"), ("it", "Procedi.", "approve"), ("it", "No.", "deny"), ("it", "Rifiuta.", "deny"),
    ("pt", "Sim.", "approve"), ("pt", "Pode.", "approve"), ("pt", "Não.", "deny"), ("pt", "Rejeita.", "deny"),
    # An approve word buried in a sentence is not a verdict. The gate re-asks.
    ("es", "si quieres luego miramos el archivo de configuración", None),
    ("fr", "oui mais je voudrais d'abord lire le fichier", None),
    ("de", "ja aber zuerst möchte ich die Datei lesen", None),
    ("es", "plátano", None), ("de", "Banane", None),
]


def test_tables() -> None:
    for lang, text, want in CONTROLS:
        got = control_intent(text, lang)
        check(f"{lang} control {text!r}", got == want, f"got={got}")
    for lang, text, want in VERDICTS:
        got = match_intent(text, lang)
        check(f"{lang} verdict {text!r}", got == want, f"got={got}")


def test_english_stays_alone() -> None:
    # The default language must behave exactly as before.
    for text in ("Pausa.", "Despierta.", "Wach auf.", "Svegliati."):
        check(f"en ignores control {text!r}", control_intent(text) is None)
    for text in ("Ja.", "Oui.", "Sim.", "Adelante.", "Nein.", "Non."):
        check(f"en ignores verdict {text!r}", match_intent(text) is None)
    check("en still pauses", control_intent("Pause listening.") == "pause")
    check("en still approves", match_intent("go ahead") == "approve")
    # Sentences stay instructions, in every language.
    check("es sentence is not a command", control_intent("Pausa el vídeo cuando termine.", "es") is None)
    check("de sentence is not a command", control_intent("Wach auf und lies die Datei.", "de") is None)


def test_english_always_works() -> None:
    for lang in LANGUAGES:
        check(f"{lang}: English still works", control_intent("wake up", lang) == "resume"
              and match_intent("approve", lang) == "approve")


def test_language_selection() -> None:
    check("en turns nothing on", active("en") == ())
    check("unknown language turns nothing on", active("ja") == ())
    check("region codes work", active("es-MX") == (LANGUAGES["es"],))
    check("auto turns everything on", len(active("auto")) == len(LANGUAGES))
    check("auto hears Spanish", control_intent("Despierta.", "auto") == "resume")
    check("auto hears German", match_intent("Nein.", "auto") == "deny")
    check("deny outranks approve across languages", match_intent("ja no", "auto") == "deny")
    check("fold drops accents and apostrophes", fold("D’Accord Réveille Faça Straße") == "daccord reveille faca strasse")


def _speech(n):
    return [(np.random.randn(480) * 0.2).astype(np.float32) for _ in range(n)]


def _silence(n):
    return [np.zeros(480, dtype=np.float32) for _ in range(n)]


async def _gate(language: str, answer: str) -> FakeBackend:
    script = [
        PermissionRequest("r1", "Bash", {"command": "npm install"}),
        AssistantTextDelta(text="Listo."),
        TurnComplete(text="Listo."),
    ]
    backend = FakeBackend(scripts=[script])
    orch = Orchestrator(
        cfg=Config(silence_ms=300, min_speech_ms=100, language=language),
        backend=backend,
        vad=EnergyVAD(sample_rate=16000, frame_samples=480, threshold=0.012),
        stt=FakeSTT(["Instala las dependencias.", answer]),
        tts=FakeTTS(),
        mic=FakeMic(_speech(15) + _silence(35) + _speech(15) + _silence(35)),
        speaker=FakeSpeaker(16000),
    )
    await asyncio.wait_for(orch.run(), timeout=10)
    return backend


async def test_round_trip() -> None:
    approved = await _gate("es", "Sí, adelante.")
    check("spoken gate approves in Spanish", [r[1] for r in approved.permission_responses] == [True], str(approved.permission_responses))
    denied = await _gate("de", "Nein.")
    check("spoken gate denies in German", [r[1] for r in denied.permission_responses] == [False], str(denied.permission_responses))


def main() -> int:
    test_tables()
    test_english_stays_alone()
    test_english_always_works()
    test_language_selection()
    asyncio.run(test_round_trip())
    ok = all(RESULTS)
    print(f"[{'PASS' if ok else 'FAIL'}] overall ({sum(RESULTS)}/{len(RESULTS)})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

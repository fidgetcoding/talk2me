"""The foreign command words through a real speech engine, from files. Mac only:
macOS `say` renders them straight to WAV. No mic, no speakers, no accounts. The
voices are synthetic, so this shows the words survive Whisper, not how it'll go
with your accent. Downloads the multilingual base model (~140 MB) on first run.

Run:  python -m tests.manual_voice_commands_check [--gpu]
"""

import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from talk2me.orchestrator import control_intent, match_intent
from talk2me.stt import WhisperSTT
from talk2me.stt.mlx_whisper import MLXWhisperSTT
from talk2me.voicecommands import bias_terms

VOICES = {
    "es": ["Monica", "Eddy (Spanish (Spain))"],
    "fr": ["Thomas", "Eddy (French (France))"],
    "de": ["Anna", "Eddy (German (Germany))"],
    "it": ["Alice", "Eddy (Italian (Italy))"],
    "pt": ["Luciana", "Eddy (Portuguese (Brazil))"],
}
COMMANDS = {
    "es": [("pausa", "pause"), ("despierta", "resume"), ("sesión de equipo", "team"), ("solo yo", "solo"), ("sí", "approve"), ("adelante", "approve"), ("no", "deny"), ("rechaza", "deny")],
    "fr": [("pause", "pause"), ("réveille-toi", "resume"), ("mode équipe", "team"), ("juste moi", "solo"), ("oui", "approve"), ("d'accord", "approve"), ("non", "deny"), ("refuse", "deny")],
    "de": [("Pause", "pause"), ("wach auf", "resume"), ("Teamsitzung", "team"), ("nur ich", "solo"), ("ja", "approve"), ("genehmigen", "approve"), ("nein", "deny"), ("ablehnen", "deny")],
    "it": [("pausa", "pause"), ("svegliati", "resume"), ("sessione di gruppo", "team"), ("solo io", "solo"), ("sì", "approve"), ("procedi", "approve"), ("no", "deny"), ("rifiuta", "deny")],
    "pt": [("pausa", "pause"), ("acorda", "resume"), ("modo equipe", "team"), ("só eu", "solo"), ("sim", "approve"), ("aprova", "approve"), ("não", "deny"), ("rejeita", "deny")],
}

# Ordinary requests, to catch the bias terms backfiring: a sentence that comes back
# as a command, or a tool approved because the engine parroted a word it was given.
SENTENCES = {
    "es": ["Abre el archivo de configuración y cambia el puerto.", "Quiero que revises las pruebas antes de seguir."],
    "fr": ["Ouvre le fichier de configuration et change le port.", "Je voudrais que tu vérifies les tests avant de continuer."],
    "de": ["Öffne die Konfigurationsdatei und ändere den Port.", "Bitte prüfe die Tests, bevor du weitermachst."],
    "it": ["Apri il file di configurazione e cambia la porta.", "Vorrei che controllassi i test prima di continuare."],
    "pt": ["Abra o arquivo de configuração e mude a porta.", "Quero que você verifique os testes antes de continuar."],
}


def _render(folder: str, voice: str, text: str, name: str) -> np.ndarray:
    aiff, wav = Path(folder, f"{name}.aiff"), Path(folder, f"{name}.wav")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), "--", text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return np.concatenate([np.zeros(4000), pcm / 32768, np.zeros(4000)]).astype(np.float32)


async def _run(make_engine) -> tuple[int, int]:
    hits = total = 0
    false_fires: list[str] = []
    with tempfile.TemporaryDirectory() as folder:
        for lang, voices in VOICES.items():
            stt = make_engine(lang)
            stt.warmup()
            lang_hits = lang_total = 0
            misses = []
            for voice in voices:
                for i, (text, want) in enumerate(COMMANDS[lang]):
                    heard = await stt.transcribe(_render(folder, voice, text, f"{lang}{i}"), 16000)
                    got = control_intent(heard, lang) or match_intent(heard, lang)
                    lang_total += 1
                    lang_hits += got == want
                    if got != want:
                        misses.append(f"{text!r} heard {heard!r}")
            print(f"{lang}: {lang_hits}/{lang_total}" + (f"  misses: {'; '.join(misses)}" if misses else ""))
            for voice in voices:
                for i, text in enumerate(SENTENCES[lang]):
                    heard = await stt.transcribe(_render(folder, voice, text, f"{lang}s{i}"), 16000)
                    fired = control_intent(heard, lang) or match_intent(heard, lang)
                    if fired not in (None, "deny"):
                        false_fires.append(f"{lang} {heard!r} -> {fired}")
                    print(f"    {lang} sentence: {heard}")
            hits += lang_hits
            total += lang_total
    print(f"sentences mistaken for a command or an approval: {len(false_fires)}", false_fires or "")
    return hits, total


def main() -> None:
    hits, total = asyncio.run(_run(lambda lang: WhisperSTT(model="base", language=lang, vocab=bias_terms(lang))))
    print(f"whisper base, CPU: {hits}/{total}")
    if "--gpu" in sys.argv:
        hits, total = asyncio.run(_run(lambda lang: MLXWhisperSTT(language=lang, vocab=bias_terms(lang))))
        print(f"large-v3-turbo, GPU: {hits}/{total}")


if __name__ == "__main__":
    main()

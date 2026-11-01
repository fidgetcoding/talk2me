"""Voice commands beyond English.

The loop's control words (pause, wake up, approve, deny, team session) live in
orchestrator.py in English, and they always work. This adds the same commands for
the languages Whisper handles cleanly and I could test: Spanish, French, German,
Italian and Portuguese. They switch on with --language, and "auto" turns on all of
them. With the default --language en nothing here is active, so a stray "ja" or
"para" can't do anything.

Everything is matched after fold(): lowercase, accents and apostrophes gone, because
Whisper is inconsistent about them ("sí"/"si", "d'accord"/"daccord"). The phrases
below are written in that folded form.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Vocab:
    pause: tuple[str, ...]
    resume: tuple[str, ...]
    sessions: tuple[str, ...]
    team: tuple[str, ...]
    solo: tuple[str, ...]
    approve: tuple[str, ...]  # single tokens or phrases
    deny: tuple[str, ...]
    cancel: tuple[str, ...]
    picks: tuple[tuple[str, ...], ...]  # index 0 is pick 1
    fillers: tuple[str, ...]


LANGUAGES: dict[str, Vocab] = {
    "es": Vocab(
        pause=("pausa", "pausar", "deja de escuchar", "para de escuchar", "duerme", "a dormir",
               "vete a dormir", "descansa", "toma un descanso"),
        resume=("despierta", "despiertate", "sigue escuchando", "vuelve a escuchar", "ya volvi",
                "ya estoy aqui", "reanuda"),
        sessions=("sesion anterior", "retoma la sesion anterior", "continua la sesion anterior",
                  "lista de sesiones", "sesiones anteriores"),
        team=("sesion de equipo", "modo equipo", "todos pueden hablar"),
        solo=("sesion individual", "modo individual", "solo yo"),
        approve=("si", "aprueba", "apruebo", "aprobar", "aprobado", "adelante", "dale", "vale", "claro",
                 "confirma", "confirmo", "confirmar", "acepta", "acepto", "aceptar", "hazlo", "permite"),
        deny=("no", "nunca", "rechaza", "rechazo", "rechazar", "deniega", "niega", "denegar", "cancela",
              "cancelar", "para", "detente", "alto"),
        cancel=("cancela", "cancelar", "olvidalo", "dejalo"),
        picks=(("uno", "una", "primero", "primera", "ultimo", "ultima"), ("dos", "segundo", "segunda"),
               ("tres", "tercero", "tercera"), ("cuatro", "cuarto", "cuarta"), ("cinco", "quinto", "quinta")),
        fillers=("oye", "bueno", "pues", "por", "favor", "eh", "ahora", "espera"),
    ),
    "fr": Vocab(
        pause=("pause", "mets en pause", "arrete decouter", "dors", "va dormir", "fais une pause"),
        resume=("reveille toi", "reprends", "reprends lecoute", "recommence a ecouter", "je suis la",
                "je suis revenu", "je suis revenue"),
        sessions=("session precedente", "reprends la session precedente", "continue la session precedente",
                  "liste des sessions"),
        team=("session dequipe", "mode equipe", "tout le monde peut parler"),
        solo=("session solo", "mode solo", "seulement moi", "juste moi"),
        approve=("oui", "approuve", "approuver", "daccord", "vas y", "allez y", "confirme", "confirmer",
                 "accepte", "accepter", "fais le", "autorise"),
        deny=("non", "refuse", "refuser", "jamais", "annule", "annuler", "arrete", "pas"),
        cancel=("annule", "annuler", "laisse tomber"),
        picks=(("un", "une", "premier", "premiere", "dernier", "derniere"), ("deux", "deuxieme", "second", "seconde"),
               ("trois", "troisieme"), ("quatre", "quatrieme"), ("cinq", "cinquieme")),
        fillers=("alors", "euh", "bon", "sil", "te", "plait", "vous", "maintenant", "attends"),
    ),
    "de": Vocab(
        pause=("pause", "pausieren", "hor auf zuzuhoren", "nicht zuhoren", "schlaf", "schlafen",
               "geh schlafen", "mach eine pause"),
        resume=("wach auf", "aufwachen", "hor wieder zu", "weiter zuhoren", "ich bin zuruck", "ich bin wieder da"),
        sessions=("vorherige sitzung", "letzte sitzung", "sitzung fortsetzen", "sitzungen auflisten",
                  "liste der sitzungen"),
        team=("team sitzung", "teamsitzung", "team modus", "teammodus", "alle durfen sprechen"),
        solo=("solo sitzung", "solositzung", "solo modus", "solomodus", "nur ich"),
        approve=("ja", "genehmigen", "genehmigt", "erlauben", "erlaubt", "bestatigen", "bestatigt", "klar",
                 "mach es", "machs", "weiter", "einverstanden", "zustimmen"),
        deny=("nein", "ablehnen", "abgelehnt", "verweigern", "stopp", "abbrechen", "niemals", "nicht", "halt"),
        cancel=("abbrechen", "vergiss es"),
        picks=(("eins", "erste", "erstes", "erster", "letzte", "letzter", "letztes"), ("zwei", "zweite", "zweites"),
               ("drei", "dritte", "drittes"), ("vier", "vierte", "viertes"), ("funf", "funfte", "funftes")),
        fillers=("also", "ah", "ahm", "bitte", "jetzt", "warte"),
    ),
    "it": Vocab(
        pause=("pausa", "metti in pausa", "smetti di ascoltare", "dormi", "vai a dormire", "fai una pausa"),
        resume=("svegliati", "riprendi", "riprendi ad ascoltare", "sono tornato", "sono tornata"),
        sessions=("sessione precedente", "riprendi la sessione precedente", "continua la sessione precedente",
                  "elenco sessioni"),
        team=("sessione di gruppo", "modalita gruppo", "tutti possono parlare"),
        solo=("sessione singola", "modalita singola", "solo io"),
        approve=("si", "approva", "approvo", "approvato", "procedi", "vai", "certo", "conferma", "confermo",
                 "accetta", "accetto", "consenti", "fallo"),
        deny=("no", "mai", "nega", "negato", "rifiuta", "rifiuto", "annulla", "ferma", "fermati", "basta"),
        cancel=("annulla", "lascia perdere"),
        picks=(("uno", "una", "primo", "prima", "ultimo", "ultima"), ("due", "secondo", "seconda"),
               ("tre", "terzo", "terza"), ("quattro", "quarto", "quarta"), ("cinque", "quinto", "quinta")),
        fillers=("ehi", "allora", "ecco", "ora", "adesso", "aspetta", "per", "favore"),
    ),
    "pt": Vocab(
        pause=("pausa", "pausar", "pare de ouvir", "para de ouvir", "dorme", "durma", "vai dormir",
               "faca uma pausa", "faz uma pausa"),
        resume=("acorda", "acorde", "volta a ouvir", "volte a ouvir", "continue ouvindo", "voltei", "estou de volta"),
        sessions=("sessao anterior", "retomar sessao anterior", "continuar sessao anterior", "lista de sessoes"),
        team=("sessao em equipe", "modo equipe", "todos podem falar"),
        solo=("sessao individual", "modo individual", "so eu", "apenas eu"),
        approve=("sim", "aprova", "aprove", "aprovado", "aprovar", "pode", "confirma", "confirme", "aceita",
                 "aceite", "claro", "faz isso", "faca isso", "vai"),
        deny=("nao", "nunca", "rejeita", "rejeite", "nega", "negue", "negar", "cancela", "cancele", "cancelar",
              "pare", "para"),
        cancel=("cancela", "cancele", "cancelar", "esquece"),
        picks=(("um", "uma", "primeiro", "primeira", "ultimo", "ultima"), ("dois", "duas", "segundo", "segunda"),
               ("tres", "terceiro", "terceira"), ("quatro", "quarto", "quarta"), ("cinco", "quinto", "quinta")),
        fillers=("ei", "entao", "bom", "agora", "espera", "por", "favor"),
    ),
}

# An approve word in a long sentence is probably not a verdict: "si quieres, luego
# miramos el README" must not run a tool. English keeps its older, looser rule.
MAX_APPROVE_WORDS = 4


# Written with their accents, because these go to the speech engine as bias terms:
# one or two-word commands are the hardest thing to hear, and Whisper heard
# "sí" as "C" and "no" as "lo" until it was told these words exist.
BIAS_TERMS: dict[str, tuple[str, ...]] = {
    "es": ("sí", "no", "adelante", "rechaza", "pausa", "despierta", "sesión de equipo", "solo yo"),
    "fr": ("oui", "non", "d'accord", "refuse", "pause", "réveille-toi", "mode équipe", "juste moi"),
    "de": ("ja", "nein", "genehmigen", "ablehnen", "Pause", "wach auf", "Teamsitzung", "nur ich"),
    "it": ("sì", "no", "procedi", "rifiuta", "pausa", "svegliati", "sessione di gruppo", "solo io"),
    "pt": ("sim", "não", "aprova", "rejeita", "pausa", "acorda", "modo equipe", "só eu"),
}


def bias_terms(language: str | None) -> list[str]:
    """Command words to bias the speech engine toward. One language only: mixing
    five languages' words into a prompt would just confuse auto-detection."""
    code = (language or "en").lower().replace("_", "-").split("-")[0]
    return list(BIAS_TERMS.get(code, ()))


def fold(text: str) -> str:
    """Lowercase, no accents, no apostrophes. 'Sí' -> 'si', 'd’accord' -> 'daccord'."""
    text = text.lower().replace("ß", "ss").replace("’", "").replace("'", "")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def active(language: str | None) -> tuple[Vocab, ...]:
    """The vocabularies --language turns on. English is separate and always on."""
    code = (language or "en").lower().replace("_", "-").split("-")[0]
    if code in ("auto", ""):
        return tuple(LANGUAGES.values())
    vocab = LANGUAGES.get(code)
    return (vocab,) if vocab else ()


def verdict(text: str, language: str | None) -> str | None:
    """'approve' / 'deny' / None for the non-English vocabularies. Deny wins."""
    vocabs = active(language)
    if not vocabs:
        return None
    words = "".join(ch if ch.isalpha() else " " for ch in fold(text)).split()
    joined = f" {' '.join(words)} "
    if any(f" {term} " in joined for v in vocabs for term in v.deny):
        return "deny"
    if len(words) <= MAX_APPROVE_WORDS and any(f" {term} " in joined for v in vocabs for term in v.approve):
        return "approve"
    return None

"""Registro delle analisi prodotte da edi.audio, tenuto fuori dai file audio.

Perché non basta un campo nel file: quando Mixxx riscrive i tag — ed è
esattamente quello che vogliamo, perché è così che le correzioni fatte a mano
tornano nel FLAC — elimina i campi che non conosce. Verificato sul campo: dopo
un «Esporta su File Tags» di Mixxx 2.5.4 il nostro campo privato non c'è più.

Se l'impronta di riferimento vive solo nel file, sparisce proprio nel momento
in cui servirebbe, e la ri-analisi successiva cancellerebbe la correzione
appena fatta. Quindi la si tiene qui.

La chiave è la firma MD5 dell'**audio decodificato**, che ogni FLAC porta nel
proprio STREAMINFO: non cambia quando si riscrivono i tag né quando il file
viene rinominato o spostato, e non richiede di rileggere il file per intero.
"""
import json
from pathlib import Path

STATE_DIR = Path.home() / ".dj_downloader"
STATE_FILE = STATE_DIR / "analysis-state.json"


def audio_key(audio) -> str:
    """Identificatore stabile di una traccia, da un oggetto `mutagen.flac.FLAC`.

    Restituisce stringa vuota se il file non ha la firma (alcuni encoder la
    lasciano a zero): in quel caso si ripiega sulla via prudente altrove.
    """
    signature = getattr(audio.info, "md5_signature", 0)
    if not signature:
        return ""
    return f"{signature:032x}"


def load() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def get(key: str) -> dict | None:
    if not key:
        return None
    return load().get(key)


def put(key: str, fingerprint: dict) -> None:
    """Registra l'impronta dell'analisi per una traccia."""
    if not key:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load()
    state[key] = fingerprint
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(STATE_FILE)

"""Interoperabilità con l'installazione locale di Mixxx.

Non si scrive nulla dentro Mixxx: si legge la sua configurazione per verificare
che sia impostato in modo da restituirci le correzioni fatte a mano, e si
generano file che Mixxx sa importare (crate).

Il round-trip funziona così:

    edi.audio scrive griglia e cue nei tag Serato del FLAC
        ↓
    Mixxx li importa all'aggiunta in libreria (track.cpp:164-264)
        ↓
    correggi in Mixxx quello che l'automatismo ha sbagliato
        ↓
    Mixxx riscrive i tag nel file, se abilitato (le due opzioni qui sotto)
        ↓
    edi.audio se ne accorge e da lì in poi non tocca più quei cue

Verificato su Mixxx 2.5.4, e con due sorprese che vale la pena conoscere:

1. La scrittura non è immediata. Mixxx la rimanda a quando la traccia non è
   più caricata in nessun deck — lo dice lui stesso in una finestra di avviso.
   In pratica: la correzione arriva nel file alla chiusura di Mixxx, oppure
   subito dopo aver espulso la traccia da tutti i deck.
2. Mixxx **elimina i campi che non conosce** quando riscrive i tag. Un'impronta
   dell'analisi salvata dentro il file sparirebbe quindi proprio nel momento in
   cui è appena avvenuta una correzione. Per questo la copia autorevole vive
   fuori dal file, in `dj/state.py`.

Mixxx definisce inoltre sperimentale la scrittura dei tag Serato e avverte che
i metadata Serato esistenti possono andare persi (`dlgpreflibrary.cpp:576`).
"""
import os
import shutil
from pathlib import Path

# Le due opzioni che servono, nella sezione [Library] di mixxx.cfg.
# `SeratoMetadataExport` da sola non basta: nella finestra delle preferenze è
# subordinata a `SyncTrackMetadata` (dlgpreflibrary.cpp:329), quindi senza la
# prima la seconda resta inerte.
SETTING_SYNC = "SyncTrackMetadata"
SETTING_SERATO = "SeratoMetadataExport"

_CONFIG_CANDIDATES = (
    Path.home() / ".mixxx" / "mixxx.cfg",
    Path.home() / ".local" / "share" / "mixxx" / "mixxx.cfg",
    Path.home() / "Library" / "Application Support" / "Mixxx" / "mixxx.cfg",
)


def is_installed() -> bool:
    return shutil.which("mixxx") is not None


def config_path() -> Path | None:
    """Percorso di `mixxx.cfg`, o `None` se Mixxx non è mai stato avviato."""
    override = os.environ.get("DJ_MIXXX_CONFIG")
    if override and Path(override).is_file():
        return Path(override)
    for candidate in _CONFIG_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def read_settings() -> dict:
    """Legge le impostazioni rilevanti da `mixxx.cfg`.

    Il formato è a righe `Chiave valore` raggruppate sotto intestazioni fra
    parentesi quadre; non è un INI standard, quindi si analizza a mano.
    """
    path = config_path()
    if path is None:
        return {}

    settings, section = {}, ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if not line or section != "Library":
                continue
            key, _, value = line.partition(" ")
            settings[key] = value.strip()
    except OSError:
        return {}
    return settings


def roundtrip_status() -> dict:
    """Se Mixxx è configurato per restituirci le correzioni fatte a mano."""
    path = config_path()
    if path is None:
        return {
            "ok": False,
            "installed": is_installed(),
            "config": None,
            "reason": "Mixxx non è mai stato avviato: manca mixxx.cfg",
            "steps": [],
        }

    settings = read_settings()
    sync = settings.get(SETTING_SYNC) == "1"
    serato = settings.get(SETTING_SERATO) == "1"

    steps = []
    if not sync:
        steps.append("Preferenze → Libreria → «Sincronizza i metadata della "
                     "libreria con quelli dei file»")
    if not serato:
        steps.append("Preferenze → Libreria → «Esporta metadata Serato» "
                     "(Mixxx avvisa che è sperimentale: è normale, conferma)")

    return {
        "ok": sync and serato,
        "installed": is_installed(),
        "config": str(path),
        "sync_track_metadata": sync,
        "serato_metadata_export": serato,
        "reason": "" if (sync and serato) else
                  "Mixxx non riscrive i tag nei file: le correzioni fatte a mano "
                  "resterebbero nel suo database e non tornerebbero in edi.audio",
        "steps": steps,
    }


def write_crates(directory, entries: list[dict]) -> list[Path]:
    """Genera le crate come file M3U8, pronte da importare in Mixxx.

    Mixxx importa una crate da un file di playlist con *tasto destro sul
    pannello Crates → Import Crate* e accetta selezioni multiple
    (`cratefeature.cpp:700-746`). Il nome della crate è quello del file, quindi
    i nomi qui sotto sono già quelli che vedrai nella barra laterale.
    """
    from . import naming

    directory = Path(directory)
    out_dir = directory / "crates"
    out_dir.mkdir(exist_ok=True)

    def m3u8(rows, title):
        lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
        for row in rows:
            label = " - ".join(x for x in (row.get("artist"), row.get("title")) if x)
            lines.append(f"#EXTINF:{int(row.get('duration') or 0)},{label}")
            lines.append(row["relpath"])
        return "\n".join(lines) + "\n"

    written = []

    # Per fascia di tempo: sono le fasce con cui si costruisce davvero un set,
    # non decine di valori di BPM distinti.
    bands = [(0, 100, "fino a 100"), (100, 118, "100-118"), (118, 126, "118-126"),
             (126, 132, "126-132"), (132, 140, "132-140"), (140, 155, "140-155"),
             (155, 175, "155-175"), (175, 999, "oltre 175")]
    for low, high, label in bands:
        rows = sorted((e for e in entries if e.get("bpm") and low <= e["bpm"] < high),
                      key=lambda e: e["bpm"])
        if rows:
            path = out_dir / f"BPM {label}.m3u8"
            path.write_text(m3u8(rows, f"BPM {label}"), encoding="utf-8")
            written.append(path)

    # Per chiave: una crate per posizione della ruota Camelot, così le tracce
    # che si mixano in armonia stanno già insieme.
    by_camelot: dict[str, list] = {}
    for entry in entries:
        if entry.get("camelot"):
            by_camelot.setdefault(entry["camelot"], []).append(entry)
    for camelot in sorted(by_camelot, key=naming.camelot_sort_key):
        rows = sorted(by_camelot[camelot], key=lambda e: e.get("bpm") or 0)
        path = out_dir / f"Camelot {camelot}.m3u8"
        path.write_text(m3u8(rows, f"Camelot {camelot}"), encoding="utf-8")
        written.append(path)

    # Per genere
    by_genre: dict[str, list] = {}
    for entry in entries:
        genre = (entry.get("genre") or "").strip()
        if genre:
            by_genre.setdefault(genre, []).append(entry)
    for genre, rows in sorted(by_genre.items()):
        rows = sorted(rows, key=lambda e: e.get("bpm") or 0)
        path = out_dir / f"{naming.sanitize(genre)}.m3u8"
        path.write_text(m3u8(rows, genre), encoding="utf-8")
        written.append(path)

    return written

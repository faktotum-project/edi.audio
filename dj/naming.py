"""Notazione Camelot e nomi di file che si ordinano da soli per BPM."""
import re
import unicodedata

# Ruota Camelot: minori = A, maggiori = B. La minore è 8A, Do maggiore è 8B.
# La chiave è la tonica in notazione anglosassone; i bemolli sono normalizzati
# a diesis perché è così che li scrivono i tag della maggior parte delle fonti.
_CAMELOT_MINOR = {
    "A": "8A", "A#": "3A", "B": "10A", "C": "5A", "C#": "12A", "D": "7A",
    "D#": "2A", "E": "9A", "F": "4A", "F#": "11A", "G": "6A", "G#": "1A",
}
_CAMELOT_MAJOR = {
    "B": "1B", "F#": "2B", "C#": "3B", "G#": "4B", "D#": "5B", "A#": "6B",
    "F": "7B", "C": "8B", "G": "9B", "D": "10B", "A": "11B", "E": "12B",
}

_FLAT_TO_SHARP = {
    "Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#",
    "Cb": "B", "Fb": "E",
}

# Ordine di mixaggio armonico: si percorre la ruota, e per ogni numero si
# mettono vicini minore e maggiore, che sono relativi fra loro e si mixano bene.
CAMELOT_WHEEL = [f"{n}{m}" for n in range(1, 13) for m in ("A", "B")]


def to_camelot(tonic: str, is_minor: bool) -> str:
    """`("A", True)` → `"8A"`. Stringa vuota se la tonica non si riconosce."""
    tonic = _FLAT_TO_SHARP.get(tonic, tonic)
    table = _CAMELOT_MINOR if is_minor else _CAMELOT_MAJOR
    return table.get(tonic, "")


def parse_key(text: str) -> tuple[str, bool] | None:
    """Legge una chiave scritta a mano o presa da un tag: `Am`, `A min`, `F#`,
    `Bbm`, `C maj`. Restituisce `(tonica, è_minore)` o `None`."""
    if not text:
        return None
    text = text.strip()
    match = re.match(r"^([A-Ga-g])([#b♯♭]?)\s*(.*)$", text)
    if not match:
        return None
    tonic = match.group(1).upper() + match.group(2).replace("♯", "#").replace("♭", "b")
    rest = match.group(3).lower().replace(" ", "")
    is_minor = rest.startswith("m") and not rest.startswith("maj")
    return _FLAT_TO_SHARP.get(tonic, tonic), is_minor


def key_name(tonic: str, is_minor: bool) -> str:
    """Forma compatta per il tag INITIALKEY: `Am`, `F#`, `C`."""
    return f"{tonic}m" if is_minor else tonic


def camelot_sort_key(camelot: str) -> tuple[int, int]:
    """Chiave d'ordinamento che segue la ruota, non l'alfabeto: 2A viene dopo
    1B, non dopo 12A."""
    match = re.match(r"^(\d+)([AB])$", camelot or "")
    if not match:
        return (99, 0)
    return (int(match.group(1)), 0 if match.group(2) == "A" else 1)


# ---------------------------------------------------------------------------
# Nomi di file
# ---------------------------------------------------------------------------

# Oltre ai caratteri vietati dai filesystem si normalizzano le varianti a
# larghezza piena, che yt-dlp usa per sostituire i due punti nei titoli e che
# altrimenti resterebbero nei nomi (`AFRO HOUSE ：`).
_FULLWIDTH = str.maketrans({"：": "-", "／": "-", "＼": "-", "｜": "-",
                            "？": "", "＊": "", "＜": "", "＞": "", "＂": "'"})
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def sanitize(text: str, fallback: str = "Sconosciuto") -> str:
    """Rende una stringa usabile come parte di nome file su qualsiasi sistema.

    I caratteri vietati diventano trattini invece di sparire, così
    "AC/DC" resta "AC-DC" e non "ACDC".
    """
    text = unicodedata.normalize("NFC", text or "")
    text = text.translate(_FULLWIDTH)
    text = _ILLEGAL.sub("-", text)
    text = _SPACES.sub(" ", text).strip(" .")
    return text or fallback


def track_filename(bpm: float | None, camelot: str, artist: str, title: str,
                   ext: str = "flac", max_bytes: int = 250, key: str = "") -> str:
    """`128 - 8A - Am - Artista - Titolo.flac`.

    Il BPM in testa e a tre cifre fisse fa sì che l'ordinamento alfabetico del
    file manager, e quindi anche quello di una chiavetta USB, sia già
    l'ordinamento per tempo. La chiave Camelot subito dopo rende leggibile a
    colpo d'occhio quali tracce si mixano bene fra loro; segue la tonalità
    musicale, leggibile anche senza conoscere la ruota Camelot.
    """
    parts = []
    if bpm:
        parts.append(f"{round(bpm):03d}")
    if camelot:
        parts.append(camelot)
    if key:
        parts.append(key)
    parts.append(sanitize(artist, "Artista sconosciuto"))
    parts.append(sanitize(title, "Senza titolo"))
    stem = " - ".join(parts)

    # Il limite dei filesystem è in byte, non in caratteri: un titolo pieno di
    # accenti o kanji sfonda il limite molto prima di quanto sembri.
    suffix = f".{ext}"
    budget = max_bytes - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8")
    if len(encoded) > budget:
        stem = encoded[:budget].decode("utf-8", "ignore").rstrip(" -")
    return stem + suffix


def deduplicate(path):
    """Restituisce un percorso libero, aggiungendo ` (2)`, ` (3)`… se serve."""
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 2
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1

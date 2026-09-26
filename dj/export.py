"""Playlist M3U8 e Rekordbox XML.

Le playlist sono il modo in cui l'ordinamento per BPM e per chiave diventa
utilizzabile: Mixxx le importa da *File → Load Playlist*, e su una chiavetta
USB si copiano così come sono.

Il Rekordbox XML porta griglia e cue anche su Rekordbox e Denon Engine. Mixxx
non lo legge — a Mixxx bastano i tag nel file — ma è anche un backup leggibile
dell'analisi, utile se un giorno i tag si perdono.
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from . import mixxx, naming, tags

LIBRARY_FILE = "libreria.json"
PLAYLIST_DIR = "playlists"


# ---------------------------------------------------------------------------
# Indice della libreria
# ---------------------------------------------------------------------------

def scan(directory) -> list[dict]:
    """Rilegge i tag di tutti i FLAC di una cartella.

    La fonte di verità è sempre il file, mai un indice a parte: se una traccia
    viene ritoccata in Mixxx o in Serato, il prossimo export lo rispecchia.
    """
    directory = Path(directory)
    entries = []
    for path in sorted(directory.rglob("*.flac")):
        try:
            info = tags.read(path)
        except Exception:
            continue
        key = naming.parse_key(info.get("key") or "")
        camelot = naming.to_camelot(*key) if key else ""
        entries.append({
            "path": str(path),
            "relpath": str(path.relative_to(directory)),
            "artist": info.get("artist") or "",
            "title": info.get("title") or "",
            "genre": info.get("genre") or "",
            "bpm": info.get("bpm"),
            "key": info.get("key") or "",
            "camelot": camelot,
            "duration": info.get("duration"),
            "cues": info.get("cues") or [],
            "loops": info.get("loops") or [],
            "bpm_locked": info.get("bpm_locked", False),
            "edited_manually": info.get("edited_manually", False),
            "source": info.get("source") or "",
            "comment": info.get("comment") or "",
            "beatgrid": info.get("beatgrid"),
        })
    return entries


def write_index(directory, entries: list[dict]) -> Path:
    """Salva un indice JSON, che è quello che la web app legge per la tabella."""
    path = Path(directory) / LIBRARY_FILE
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Playlist M3U8
# ---------------------------------------------------------------------------

def _m3u8(entries: list[dict], title: str) -> str:
    lines = ["#EXTM3U", f"#PLAYLIST:{title}"]
    for e in entries:
        seconds = int(e.get("duration") or 0)
        label = " - ".join(x for x in (e.get("artist"), e.get("title")) if x)
        annotation = " ".join(x for x in (
            f"[{round(e['bpm'])} BPM]" if e.get("bpm") else "",
            f"[{e['camelot']}]" if e.get("camelot") else "") if x)
        lines.append(f"#EXTINF:{seconds},{label} {annotation}".rstrip())
        lines.append(e["relpath"])
    return "\n".join(lines) + "\n"


def write_playlists(directory, entries: list[dict]) -> list[Path]:
    """Genera le playlist ordinate. Percorsi relativi, così la cartella resta
    trasportabile su una chiavetta senza rompere i riferimenti."""
    directory = Path(directory)
    out_dir = directory / PLAYLIST_DIR
    out_dir.mkdir(exist_ok=True)

    by_bpm = sorted(entries, key=lambda e: (e.get("bpm") or 0, e.get("artist") or ""))
    # L'ordine della ruota Camelot mette vicine le tracce che si mixano bene:
    # a parità di chiave si prosegue per BPM crescente.
    by_key = sorted(entries, key=lambda e: (naming.camelot_sort_key(e.get("camelot", "")),
                                            e.get("bpm") or 0))
    recent = sorted(entries, key=lambda e: e.get("path", ""), reverse=True)

    written = []
    for name, rows, label in (
        ("per-bpm.m3u8", by_bpm, "edi.audio — per BPM"),
        ("per-camelot.m3u8", by_key, "edi.audio — ruota Camelot"),
        ("recenti.m3u8", recent, "edi.audio — recenti"),
    ):
        path = out_dir / name
        path.write_text(_m3u8(rows, label), encoding="utf-8")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Rekordbox XML
# ---------------------------------------------------------------------------

def write_rekordbox_xml(directory, entries: list[dict]) -> Path:
    """Scrive `rekordbox.xml` con griglia, cue e loop.

    Rekordbox misura le posizioni in secondi e vuole la griglia come una serie
    di marche `TEMPO`; i cue sono `POSITION_MARK`, con `Type` 0 per un hot cue
    e 4 per un loop (che porta anche `End`).
    """
    directory = Path(directory)
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="edi.audio", Version="1.0", Company="edi.audio")

    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(entries)))
    for track_id, entry in enumerate(entries, start=1):
        attrs = {
            "TrackID": str(track_id),
            "Name": entry.get("title") or "",
            "Artist": entry.get("artist") or "",
            "Kind": "FLAC File",
            "TotalTime": str(int(entry.get("duration") or 0)),
            # Rekordbox vuole un URL file://, con il percorso codificato
            "Location": "file://localhost" + quote(str(Path(entry["path"]).resolve())),
        }
        if entry.get("bpm"):
            attrs["AverageBpm"] = f"{entry['bpm']:.2f}"
        if entry.get("key"):
            attrs["Tonality"] = entry["key"]
        if entry.get("comment"):
            attrs["Comments"] = entry["comment"]

        node = ET.SubElement(collection, "TRACK", **attrs)

        grid = entry.get("beatgrid") or {}
        if grid.get("bpm"):
            ET.SubElement(node, "TEMPO",
                          Inizio=f"{grid.get('first_beat', 0.0):.3f}",
                          Bpm=f"{grid['bpm']:.2f}",
                          Metro="4/4", Battito="1")

        for cue in entry.get("cues") or []:
            ET.SubElement(node, "POSITION_MARK",
                          Name=cue.get("label", ""),
                          Type="0",
                          Start=f"{cue['position_ms'] / 1000:.3f}",
                          Num=str(cue["index"] - 1))
        for loop in entry.get("loops") or []:
            ET.SubElement(node, "POSITION_MARK",
                          Name=loop.get("label", ""),
                          Type="4",
                          Start=f"{loop['start_ms'] / 1000:.3f}",
                          End=f"{loop['end_ms'] / 1000:.3f}",
                          Num=str(loop["index"] - 1))

    playlists = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists, "NODE", Type="0", Name="ROOT", Count="2")

    by_bpm = sorted(range(len(entries)), key=lambda i: entries[i].get("bpm") or 0)
    by_key = sorted(range(len(entries)),
                    key=lambda i: naming.camelot_sort_key(entries[i].get("camelot", "")))
    for name, order in (("per BPM", by_bpm), ("ruota Camelot", by_key)):
        node = ET.SubElement(root_node, "NODE", Name=name, Type="1",
                             KeyType="0", Entries=str(len(order)))
        for i in order:
            ET.SubElement(node, "TRACK", Key=str(i + 1))

    ET.indent(root, space="  ")
    path = directory / "rekordbox.xml"
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return path


UNSORTED_DIR = "Senza genere"


def organize_by_genre(directory, entries: list[dict]) -> list[dict]:
    """Sposta i file in una sottocartella per genere.

    Le tracce di cui non conosciamo il genere finiscono in una cartella a
    parte invece di restare sparse alla radice: così è evidente a colpo
    d'occhio quali hanno bisogno che il genere venga messo a mano.

    I nomi dei file non cambiano — restano ordinabili per BPM dentro ogni
    cartella — e le playlist vengono rigenerate dopo lo spostamento, quindi i
    riferimenti restano validi.
    """
    directory = Path(directory)
    moved = []

    for entry in entries:
        source = Path(entry["path"])
        if not source.is_file():
            continue
        folder = naming.sanitize(entry.get("genre") or "", UNSORTED_DIR)
        target_dir = directory / folder
        target = target_dir / source.name

        if source.parent.resolve() == target_dir.resolve():
            moved.append(entry)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        target = naming.deduplicate(target)
        source.rename(target)
        entry = dict(entry, path=str(target), relpath=str(target.relative_to(directory)))
        moved.append(entry)

    # Le cartelle rimaste vuote dopo lo spostamento non servono a nessuno
    for path in directory.iterdir():
        if path.is_dir() and path.name not in (PLAYLIST_DIR, "crates") and not any(path.iterdir()):
            path.rmdir()

    return moved


def export_all(directory, *, organize: bool = False) -> dict:
    """Rigenera indice, playlist, crate e XML per una cartella.

    Con `organize` i file vengono anche spostati nelle cartelle per genere.
    Lo spostamento avviene *prima* di scrivere playlist e crate, così i
    percorsi che finiscono nei file sono già quelli definitivi.
    """
    entries = scan(directory)
    if organize:
        entries = organize_by_genre(directory, entries)

    index = write_index(directory, entries)
    playlists = write_playlists(directory, entries)
    crates = mixxx.write_crates(directory, entries)
    xml = write_rekordbox_xml(directory, entries)

    return {
        "tracks": len(entries),
        "index": str(index),
        "playlists": [str(p) for p in playlists],
        "crates": [str(p) for p in crates],
        "rekordbox_xml": str(xml),
        "organized": organize,
    }

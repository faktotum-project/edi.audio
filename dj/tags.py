"""Scrittura dei tag nel FLAC: metadata, ReplayGain, beatgrid e cue Serato.

I nomi dei campi Xiph sono quelli che Mixxx cerca — vedi
`mixxx-main/src/track/taglib/trackmetadata_xiph.cpp:40-42` per i tre campi
Serato e `:487-510` per il punto in cui li rilegge.
"""
import json
from pathlib import Path

from mutagen.flac import FLAC, Picture

from . import naming, serato, state

# Nomi dei campi Serato in un file FLAC. Attenzione: in FLAC il campo dei
# marker si chiama SERATO_MARKERS_V2, in Ogg SERATO_MARKERS2 — la differenza è
# reale e sbagliarla rende il tag invisibile a Mixxx.
FIELD_BEATGRID = "SERATO_BEATGRID"
FIELD_MARKERS2 = "SERATO_MARKERS_V2"

# Impronta di quello che *noi* abbiamo scritto l'ultima volta, scritta anche
# nel file per comodità (rende il file autodescrittivo se lo si sposta su
# un'altra macchina). Non ci si può però fare affidamento: Mixxx elimina i
# campi che non conosce quando riscrive i tag, quindi la copia autorevole sta
# fuori dal file, in `dj/state.py`.
FIELD_FINGERPRINT = "EDI_AUDIO_ANALYSIS"


def write(path, *, analysis: dict, layout, quality_report=None,
          metadata: dict | None = None, cover: bytes | None = None,
          replay_gain: dict | None = None, source: str = "",
          preserve_cues: bool = False) -> dict:
    """Scrive tutto quello che serve a Mixxx per non dover rianalizzare nulla.

    Con `preserve_cues` si aggiornano metadata, guadagno e nome ma si lasciano
    intatti griglia e cue già presenti nel file: è il caso in cui li hai
    corretti a mano e una ri-analisi li distruggerebbe.

    Restituisce un riepilogo di quello che è stato scritto, che finisce nella
    cronologia e in UI.
    """
    path = Path(path)
    audio = FLAC(str(path))
    metadata = metadata or {}

    # -- metadata di base --------------------------------------------------
    for field, value in (
        ("ARTIST", metadata.get("artist")),
        ("TITLE", metadata.get("title")),
        ("ALBUM", metadata.get("album")),
        ("DATE", metadata.get("date")),
        ("GENRE", metadata.get("genre")),
    ):
        if value:
            audio[field] = str(value)

    # -- BPM e chiave ------------------------------------------------------
    bpm = float(analysis.get("bpm") or 0.0)
    summary = {"bpm": round(bpm, 2) if bpm else None, "camelot": "", "key": ""}
    if bpm:
        audio["BPM"] = f"{bpm:.2f}"

    key = analysis.get("key") or {}
    camelot = ""
    if key.get("tonic"):
        key_str = naming.key_name(key["tonic"], key["is_minor"])
        camelot = naming.to_camelot(key["tonic"], key["is_minor"])
        audio["INITIALKEY"] = key_str
        # Mixxx legge anche KEY come ripiego, e i tag scritti da altri
        # programmi finiscono spesso lì.
        audio["KEY"] = key_str
        summary["key"] = key_str
        summary["camelot"] = camelot

    # Da dove viene l'audio e com'era prima della conversione. Si conserva nel
    # file perché una volta dentro un FLAC la sorgente non è più deducibile:
    # rimisurando si otterrebbe una stima peggiore di quella fatta al momento
    # del download, quando il codec di origine era ancora noto.
    if source:
        audio["SOURCE"] = source
        summary["source"] = source

    # Un commento leggibile a colpo d'occhio nella lista tracce di Mixxx
    bits = [b for b in (camelot, f"{round(bpm)} BPM" if bpm else "") if b]
    if source:
        bits.append(source)
    elif quality_report is not None:
        bits.append(quality_report.label())
    audio["COMMENT"] = " · ".join(bits + ["edi.audio"])

    # -- ReplayGain --------------------------------------------------------
    if replay_gain:
        audio["REPLAYGAIN_TRACK_GAIN"] = f"{replay_gain['gain_db']:.2f} dB"
        audio["REPLAYGAIN_TRACK_PEAK"] = f"{replay_gain['peak']:.6f}"
        summary["gain_db"] = replay_gain["gain_db"]

    if preserve_cues:
        # Non si tocca nulla di quello che riguarda griglia e cue, nemmeno il
        # BPM scritto sopra: se la griglia è stata corretta a mano, il BPM che
        # le corrisponde è quello nel file, non quello appena ricalcolato.
        existing = _read_grid(audio)
        if existing and existing.get("bpm"):
            audio["BPM"] = f"{existing['bpm']:.2f}"
            summary["bpm"] = round(existing["bpm"], 2)
        summary["preserved"] = True
    else:
        # -- Beatgrid Serato -----------------------------------------------
        # La griglia è a tempo fisso quando gli intervalli fra beat sono
        # stabili, cioè quasi sempre nella musica da club. Quando non lo è si
        # emette una marca per ogni tratto, così la griglia segue il brano.
        grid = _build_beatgrid(analysis)
        if grid is not None:
            blob = grid.dump(serato.FLAC)
            if blob:
                audio[FIELD_BEATGRID] = blob.decode("ascii")
                summary["beatgrid_markers"] = len(grid.markers)

        # -- Hot cue e loop ------------------------------------------------
        markers = layout.to_markers2(bpm_locked=True)
        blob = markers.dump(serato.FLAC)
        if blob:
            audio[FIELD_MARKERS2] = blob.decode("ascii")
        summary["cues"] = len(layout.cues)
        summary["loops"] = len(layout.loops)
        summary["bpm_locked"] = True

        # Impronta di quello che abbiamo appena scritto, per riconoscere in
        # futuro se qualcuno l'ha cambiato
        fingerprint = _fingerprint_from(audio)
        audio[FIELD_FINGERPRINT] = json.dumps(fingerprint, separators=(",", ":"))
        state.put(state.audio_key(audio), fingerprint)

    # -- Copertina ---------------------------------------------------------
    if cover:
        picture = Picture()
        picture.type = 3           # front cover
        picture.mime = _image_mime(cover)
        picture.data = cover
        audio.clear_pictures()
        audio.add_picture(picture)
        summary["cover"] = True

    audio.save()
    return summary


# ---------------------------------------------------------------------------
# Riconoscimento delle correzioni manuali
# ---------------------------------------------------------------------------

def _read_grid(audio) -> dict | None:
    """Griglia attualmente scritta nel file."""
    values = audio.get(FIELD_BEATGRID)
    if not values:
        return None
    try:
        grid = serato.BeatGrid.parse(values[0].encode("ascii"), serato.FLAC)
    except Exception:
        return None
    terminal = grid.markers[-1]
    return {"bpm": terminal.bpm, "first_beat": grid.markers[0].position_secs,
            "markers": len(grid.markers)}


def _fingerprint_from(audio) -> dict:
    """Riassunto compatto di griglia e cue presenti nel file.

    Si confrontano i valori decodificati e non i byte grezzi: Mixxx riscrive i
    tag con un'allocazione e byte di riempimento propri, e un confronto binario
    segnalerebbe come «corretta a mano» ogni traccia semplicemente caricata.
    """
    grid = _read_grid(audio) or {}
    fingerprint = {
        "bpm": round(grid.get("bpm") or 0, 2),
        "first_beat": round(grid.get("first_beat") or 0, 3),
        "cues": [],
        "loops": [],
    }

    values = audio.get(FIELD_MARKERS2)
    if values:
        try:
            markers = serato.Markers2.parse(values[0].encode("ascii"), serato.FLAC)
        except Exception:
            return fingerprint
        # Tolleranza di 10 ms: sotto quella soglia è rumore di arrotondamento
        # fra le conversioni, non una correzione.
        fingerprint["cues"] = sorted(
            (c.index, round(c.position_ms / 10)) for c in markers.cues())
        fingerprint["loops"] = sorted(
            (l.index, round(l.start_ms / 10), round(l.end_ms / 10)) for l in markers.loops())
    return fingerprint


def manual_edits(path) -> dict | None:
    """Descrive le correzioni fatte fuori da edi.audio, o `None` se non ce ne sono.

    L'impronta di riferimento si cerca prima nel registro esterno e poi nel
    file: il campo nel file sparisce quando Mixxx riscrive i tag, cioè proprio
    nel momento in cui una correzione è appena avvenuta.

    Se non si trova alcuna impronta ma il file contiene già cue o una griglia,
    si risponde comunque «modificato». È una scelta prudente e voluta: di
    quel lavoro non sappiamo l'origine — può venire da Mixxx, da Serato o da
    una versione precedente di edi.audio — e cancellarlo sarebbe il danno
    peggiore che questa pipeline possa fare. Chi vuole rifarlo da capo ha
    `--force`.
    """
    audio = FLAC(str(path))
    current = _fingerprint_from(audio)

    previous = state.get(state.audio_key(audio))
    if previous is None:
        stored = audio.get(FIELD_FINGERPRINT)
        if stored:
            try:
                previous = json.loads(stored[0])
            except (json.JSONDecodeError, IndexError):
                previous = None

    if previous is None:
        if current["cues"] or current["loops"] or current["bpm"]:
            return {"changed": ["griglia o cue di origine sconosciuta, conservati"],
                    "current": current}
        return None
    # Il JSON rilegge le liste come liste, non come tuple
    previous["cues"] = [list(x) for x in previous.get("cues", [])]
    previous["loops"] = [list(x) for x in previous.get("loops", [])]
    current_cmp = dict(current, cues=[list(x) for x in current["cues"]],
                       loops=[list(x) for x in current["loops"]])
    if previous == current_cmp:
        return None

    changed = []
    if abs(previous.get("bpm", 0) - current["bpm"]) > 0.01:
        changed.append(f"BPM {previous.get('bpm')} → {current['bpm']}")
    if abs(previous.get("first_beat", 0) - current["first_beat"]) > 0.01:
        changed.append("primo beat della griglia spostato")
    if previous["cues"] != current_cmp["cues"]:
        changed.append(f"hot cue: {len(previous['cues'])} → {len(current['cues'])} o riposizionati")
    if previous["loops"] != current_cmp["loops"]:
        changed.append(f"loop: {len(previous['loops'])} → {len(current['loops'])} o riposizionati")

    return {"changed": changed or ["griglia o cue modificati"], "current": current}


def _build_beatgrid(analysis: dict) -> serato.BeatGrid | None:
    """Griglia Serato a partire dall'analisi.

    Il primo marker va sul primo *downbeat*, non sul primo beat: Serato e Mixxx
    disegnano la griglia a partire da lì, e una griglia sfasata di un beat
    rende inutilizzabili i loop da 16 battute.
    """
    bpm = float(analysis.get("bpm") or 0.0)
    first_beat = float(analysis.get("first_beat") or 0.0)
    if not bpm:
        return None

    if analysis.get("tempo_stable", True):
        return serato.BeatGrid.constant_tempo(first_beat, bpm)

    # Tempo variabile: una marca ogni 32 battute, con il tempo locale misurato
    # sull'intervallo reale fra le marche.
    downbeats = analysis.get("downbeat_times") or []
    if len(downbeats) < 4:
        return serato.BeatGrid.constant_tempo(first_beat, bpm)

    step = 8  # 8 downbeat = 32 battute
    markers = []
    anchors = downbeats[::step]
    for i, position in enumerate(anchors[:-1]):
        beats = (min((i + 1) * step, len(downbeats) - 1) - i * step) * 4
        markers.append(serato.BeatGridMarker(position_secs=position, beats_till_next=beats))
    # L'ultimo tratto prosegue a tempo costante fino a fine brano
    markers.append(serato.BeatGridMarker(position_secs=anchors[-1], bpm=bpm))
    return serato.BeatGrid(markers=markers)


def _image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def read(path) -> dict:
    """Rilegge quello che abbiamo scritto. È lo strumento di verifica: se
    questo non ritorna i valori attesi, non ha senso aprire Mixxx."""
    audio = FLAC(str(path))

    def first(field):
        values = audio.get(field)
        return values[0] if values else None

    result = {
        "artist": first("ARTIST"),
        "title": first("TITLE"),
        "genre": first("GENRE"),
        "album": first("ALBUM"),
        "bpm": float(first("BPM")) if first("BPM") else None,
        "key": first("INITIALKEY"),
        "comment": first("COMMENT"),
        "replay_gain": first("REPLAYGAIN_TRACK_GAIN"),
        "source": first("SOURCE"),
        "edited_manually": manual_edits(path) is not None,
        "duration": audio.info.length,
        "sample_rate": audio.info.sample_rate,
        "bits_per_sample": audio.info.bits_per_sample,
        "has_cover": bool(audio.pictures),
        "cues": [],
        "loops": [],
        "bpm_locked": False,
        "beatgrid": None,
    }

    raw_grid = first(FIELD_BEATGRID)
    if raw_grid:
        grid = serato.BeatGrid.parse(raw_grid.encode("ascii"), serato.FLAC)
        terminal = grid.markers[-1]
        result["beatgrid"] = {
            "markers": len(grid.markers),
            "first_beat": round(grid.markers[0].position_secs, 4),
            "bpm": round(terminal.bpm, 3) if terminal.bpm else None,
        }

    raw_markers = first(FIELD_MARKERS2)
    if raw_markers:
        markers = serato.Markers2.parse(raw_markers.encode("ascii"), serato.FLAC)
        result["bpm_locked"] = markers.is_bpm_locked()
        result["cues"] = [
            {"index": c.index + 1, "position_ms": c.position_ms,
             "label": c.label, "color": "#%02X%02X%02X" % c.color}
            for c in markers.cues()
        ]
        result["loops"] = [
            {"index": l.index + 1, "start_ms": l.start_ms, "end_ms": l.end_ms, "label": l.label}
            for l in markers.loops()
        ]
    return result

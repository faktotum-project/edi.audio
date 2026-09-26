"""Serializzazione dei tag Serato letti da Mixxx.

Formato ricavato dal sorgente Mixxx in `mixxx-main/`, che è la specifica
autoritativa perché è il parser che dovrà rileggere quello che scriviamo:

  - `src/track/serato/beatgrid.cpp:382-421`  (dump della beatgrid)
  - `src/track/serato/markers2.cpp:505-572`  (dump dei marker)
  - `src/track/serato/markers2.cpp:218-243`  (entry CUE)
  - `src/track/serato/markers2.cpp:797`      (variante FLAC)

Tutti i valori multi-byte sono big-endian. Le posizioni dei cue sono in
millisecondi; quelle della beatgrid in secondi come float32.

I test in `tests/test_serato.py` validano questo modulo contro i blob reali
prodotti da Serato che stanno in `mixxx-main/src/test/serato/data/`.
"""
import base64
import struct
from dataclasses import dataclass, field

# Prefisso dei blob base64 (FLAC/MP4). In C++ è dichiarato con sizeof(), che
# include il NUL finale della stringa letterale: da qui i due \x00 dopo
# "octet-stream" più quello che chiude il nome del tag.
_PREFIX_BEATGRID = b"application/octet-stream\x00\x00Serato BeatGrid\x00"
_PREFIX_MARKERS2 = b"application/octet-stream\x00\x00Serato Markers2\x00"

# Serato spezza il base64 in righe da 72 caratteri, cioè blocchi di 54 byte
# grezzi. Va riprodotto: il parser di Mixxx rimuove i newline, ma un round-trip
# byte-identico è la nostra unica prova che l'encoder sia corretto.
_B64_BLOCK = 54

FLAC = "flac"
MP3 = "mp3"
MP4 = "mp4"
OGG = "ogg"

# Palette hot cue di Serato DJ Pro (src/util/color/predefinedcolorpalettes.cpp:97+).
# Usiamo i colori che Serato e Mixxx già conoscono, così i cue appaiono con la
# tinta attesa invece che con un colore fuori palette.
COLOR_RED = (0xC0, 0x26, 0x26)
COLOR_ORANGE = (0xDB, 0x4E, 0x27)
COLOR_GREEN = (0x4E, 0xB6, 0x48)
COLOR_BLUE = (0x2B, 0x36, 0x73)
COLOR_CYAN = (0x1D, 0xBE, 0xBD)
COLOR_MAGENTA = (0xCE, 0x35, 0x9E)

# Serato colora tutti i loop salvati allo stesso modo.
COLOR_LOOP = (0x27, 0xAA, 0xE1)


def _b64encode(data: bytes, chop_padding: bool) -> bytes:
    """Base64 alla maniera di Serato: blocchi da 54 byte separati da newline.

    `chop_padding` riproduce la stranezza per cui Serato, quando l'ultimo
    blocco richiederebbe padding, taglia via un byte del base64 (markers2.cpp:37).
    """
    out = bytearray()
    for offset in range(0, len(data), _B64_BLOCK):
        if offset:
            out += b"\n"
        block = data[offset:offset + _B64_BLOCK]
        encoded = base64.b64encode(block).rstrip(b"=")
        out += encoded
        if chop_padding and len(block) % 3:
            del out[-1]
    return bytes(out)


def _b64decode(data: bytes) -> bytes:
    """Inverso di `_b64encode`.

    Ripulisce i newline e il padding di zeri, e tollera il byte tagliato da
    `chop_padding`: quel taglio mangia solo bit del `\\x00` terminale, che
    riscriviamo comunque, quindi si può scartare senza perdere dati.
    """
    raw = data.replace(b"\n", b"").replace(b"\x00", b"")
    if len(raw) % 4 == 1:
        raw = raw[:-1]
    return base64.b64decode(raw + b"=" * (-len(raw) % 4))


# ---------------------------------------------------------------------------
# Beatgrid
# ---------------------------------------------------------------------------

@dataclass
class BeatGridMarker:
    """Un tratto di griglia che inizia a `position_secs`.

    I marker non-terminali dichiarano quante battute mancano al marker
    successivo (il tempo si ricava dividendo); il marker terminale, che è
    sempre l'ultimo, dichiara direttamente il BPM da lì a fine brano.
    """
    position_secs: float
    beats_till_next: int | None = None   # solo non-terminale
    bpm: float | None = None             # solo terminale

    @property
    def is_terminal(self) -> bool:
        return self.bpm is not None

    def dump(self) -> bytes:
        if self.is_terminal:
            return struct.pack(">ff", self.position_secs, self.bpm)
        return struct.pack(">fI", self.position_secs, self.beats_till_next)


@dataclass
class BeatGrid:
    markers: list[BeatGridMarker] = field(default_factory=list)
    footer: int = 0
    extra_base64_byte: bytes = b"A"

    @classmethod
    def constant_tempo(cls, first_beat_secs: float, bpm: float) -> "BeatGrid":
        """Griglia a tempo fisso: un solo marker terminale.

        È il caso della quasi totalità della musica da club, prodotta a click.
        """
        return cls(markers=[BeatGridMarker(position_secs=first_beat_secs, bpm=bpm)])

    def dump_id3(self) -> bytes:
        if not self.markers or not self.markers[-1].is_terminal:
            return b""
        out = struct.pack(">HI", 0x0100, len(self.markers))
        for marker in self.markers:
            out += marker.dump()
        return out + struct.pack(">B", self.footer)

    def dump_base64(self) -> bytes:
        body = self.dump_id3()
        if not body:
            return b""
        return _b64encode(_PREFIX_BEATGRID + body, chop_padding=False) + self.extra_base64_byte

    def dump(self, file_type: str = FLAC) -> bytes:
        if file_type in (MP3, "aiff"):
            return self.dump_id3()
        if file_type in (FLAC, MP4):
            return self.dump_base64()
        raise ValueError(f"beatgrid non supportata per {file_type}")

    @classmethod
    def parse(cls, data: bytes, file_type: str = FLAC) -> "BeatGrid":
        extra = b"A"
        if file_type in (FLAC, MP4):
            # L'ultimo carattere è un byte di scarto che non porta dati, ma va
            # conservato per poter riprodurre il blob identico.
            extra = data[-1:]
            body = _b64decode(data[:-1])
            if not body.startswith(_PREFIX_BEATGRID):
                raise ValueError("prefisso beatgrid mancante")
            body = body[len(_PREFIX_BEATGRID):]
        else:
            body = data

        version, count = struct.unpack_from(">HI", body, 0)
        if version != 0x0100:
            raise ValueError(f"versione beatgrid inattesa: {version:#06x}")

        markers = []
        pos = 6
        for i in range(count):
            chunk = body[pos:pos + 8]
            if i == count - 1:
                secs, bpm = struct.unpack(">ff", chunk)
                markers.append(BeatGridMarker(position_secs=secs, bpm=bpm))
            else:
                secs, beats = struct.unpack(">fI", chunk)
                markers.append(BeatGridMarker(position_secs=secs, beats_till_next=beats))
            pos += 8
        return cls(markers=markers, footer=body[pos], extra_base64_byte=extra)

    def beat_positions_secs(self, track_length_secs: float) -> list[float]:
        """Espande la griglia nella lista dei singoli beat.

        Serve per verificare che il primo e l'ultimo beat cadano dove ci
        aspettiamo: un BPM giusto con la fase sbagliata suona comunque male.
        """
        beats: list[float] = []
        for i, marker in enumerate(self.markers):
            if marker.is_terminal:
                interval = 60.0 / marker.bpm
                pos = marker.position_secs
                while pos <= track_length_secs:
                    beats.append(pos)
                    pos += interval
            else:
                nxt = self.markers[i + 1].position_secs
                span = marker.beats_till_next
                if span <= 0:
                    continue
                interval = (nxt - marker.position_secs) / span
                for n in range(span):
                    beats.append(marker.position_secs + n * interval)
        return beats


# ---------------------------------------------------------------------------
# Markers2 — hot cue, loop, lock del BPM, colore traccia
# ---------------------------------------------------------------------------

class Marker2Entry:
    """Base delle entry di Markers2. Ogni sottoclasse dichiara nome e payload."""

    name: str = ""
    """Nome ASCII del tipo, scritto nel tag prima del payload."""

    def dump(self) -> bytes:
        raise NotImplementedError


@dataclass
class CueEntry(Marker2Entry):
    """Hot cue. Layout da markers2.cpp:218-243."""
    index: int
    position_ms: int
    color: tuple[int, int, int] = COLOR_RED
    label: str = ""

    name = "CUE"

    def dump(self) -> bytes:
        r, g, b = self.color
        return (
            struct.pack(">BBIBBBBBB", 0, self.index, self.position_ms, 0, r, g, b, 0, 0)
            + self.label.encode("utf-8")
            + b"\x00"
        )


@dataclass
class LoopEntry(Marker2Entry):
    """Loop salvato (markers2.cpp:331-359).

    Il blocco 0xFFFFFFFF 0x00 fra la posizione finale e il colore è costante e
    Mixxx se lo aspetta tale (markers2.cpp:11-13).
    """
    index: int
    start_ms: int
    end_ms: int
    color: tuple[int, int, int] = COLOR_LOOP
    locked: bool = False
    label: str = ""

    name = "LOOP"

    def dump(self) -> bytes:
        r, g, b = self.color
        return (
            struct.pack(">BBII", 0, self.index, self.start_ms, self.end_ms)
            + b"\xff\xff\xff\xff\x00"
            + struct.pack(">BBBBB", r, g, b, 0, int(self.locked))
            + self.label.encode("utf-8")
            + b"\x00"
        )


@dataclass
class BpmLockEntry(Marker2Entry):
    """Blocca il BPM. Con questa attiva Mixxx non rianalizza il brano e non
    sovrascrive la griglia che abbiamo calcolato (analyzerbeats.cpp:132)."""
    locked: bool = True

    name = "BPMLOCK"

    def dump(self) -> bytes:
        return struct.pack(">B", int(self.locked))


@dataclass
class ColorEntry(Marker2Entry):
    """Colore della traccia nella lista."""
    color: tuple[int, int, int]

    name = "COLOR"

    def dump(self) -> bytes:
        r, g, b = self.color
        return struct.pack(">BBBB", 0, r, g, b)


_ENTRY_TYPES = {"CUE": CueEntry, "LOOP": LoopEntry, "BPMLOCK": BpmLockEntry, "COLOR": ColorEntry}


@dataclass
class UnknownEntry(Marker2Entry):
    """Entry di un tipo che non gestiamo (Serato ne scrive altri, FLIP su tutti).

    La conserviamo grezza: riscrivere il tag non deve distruggere lavoro fatto
    in Serato solo perché noi non sappiamo interpretarlo.
    """
    raw_name: str
    payload: bytes

    @property
    def name(self) -> str:
        return self.raw_name

    def dump(self) -> bytes:
        return self.payload


@dataclass
class Markers2:
    entries: list[Marker2Entry] = field(default_factory=list)
    allocated_size: int = 0
    last_base64_byte_flac: bytes = b"A"

    def _inner(self) -> bytes:
        """Blocco dati vero: versione, entry concatenate, terminatore."""
        body = struct.pack(">H", 0x0101)
        for entry in self.entries:
            payload = entry.dump()
            body += entry.name.encode("ascii") + b"\x00" + struct.pack(">I", len(payload)) + payload
        return body + b"\x00"

    def _pad(self, outer: bytes) -> bytes:
        """Serato pre-alloca almeno 470 byte per non frammentare il file quando
        si aggiungono cue in seguito (markers2.cpp:752-758)."""
        size = self.allocated_size
        if size <= len(outer):
            size = max(len(outer) + 1, 470)
        return outer.ljust(size, b"\x00")

    def dump_common(self) -> bytes:
        """Forma Ogg: solo il blocco dati in base64."""
        if not self.entries and not self.allocated_size:
            return b""
        return _b64encode(self._inner(), chop_padding=True)

    def dump_id3(self) -> bytes:
        """Forma MP3/AIFF: header non codificato più il blocco in base64."""
        if not self.entries and not self.allocated_size:
            return b""
        return self._pad(b"\x01\x01" + _b64encode(self._inner(), chop_padding=True))

    def dump_base64(self) -> bytes:
        """Forma MP4: doppio base64, con prefisso MIME (markers2.cpp:749-795)."""
        if not self.entries and not self.allocated_size:
            return b""
        outer = _PREFIX_MARKERS2 + b"\x01\x01" + _b64encode(self._inner(), chop_padding=True)
        return _b64encode(self._pad(outer), chop_padding=False)

    def dump_flac(self) -> bytes:
        """Come MP4, ma l'ultimo byte è di scarto e va conservato tale e quale."""
        data = self.dump_base64()
        if not data:
            return b""
        return data[:-1] + self.last_base64_byte_flac

    def dump(self, file_type: str = FLAC) -> bytes:
        if file_type in (MP3, "aiff"):
            return self.dump_id3()
        if file_type == FLAC:
            return self.dump_flac()
        if file_type == MP4:
            return self.dump_base64()
        if file_type == OGG:
            return self.dump_common()
        raise ValueError(f"markers2 non supportati per {file_type}")

    @classmethod
    def parse(cls, data: bytes, file_type: str = FLAC) -> "Markers2":
        last_byte = b"A"
        allocated = 0

        if file_type in (FLAC, MP4):
            if file_type == FLAC:
                last_byte = data[-1:]
            decoded = _b64decode(data)
            if not decoded.startswith(_PREFIX_MARKERS2):
                raise ValueError("prefisso markers2 mancante")
            allocated = len(decoded)
            outer = decoded[len(_PREFIX_MARKERS2):]
            if outer[:2] != b"\x01\x01":
                raise ValueError("header markers2 esterno inatteso")
            inner = _b64decode(outer[2:])
        elif file_type == OGG:
            inner = _b64decode(data)
        else:
            if data[:2] != b"\x01\x01":
                raise ValueError("header markers2 esterno inatteso")
            allocated = len(data)
            inner = _b64decode(data[2:])

        version = struct.unpack_from(">H", inner, 0)[0]
        if version != 0x0101:
            raise ValueError(f"versione markers2 inattesa: {version:#06x}")

        entries: list[Marker2Entry] = []
        pos = 2
        while pos < len(inner):
            end = inner.find(b"\x00", pos)
            if end == -1 or end == pos:
                break  # nome vuoto: fine delle entry, inizio del padding
            name = inner[pos:end].decode("ascii", "replace")
            pos = end + 1
            if pos + 4 > len(inner):
                break
            length = struct.unpack_from(">I", inner, pos)[0]
            pos += 4
            payload = inner[pos:pos + length]
            pos += length
            entries.append(_parse_entry(name, payload))

        return cls(entries=entries, allocated_size=allocated, last_base64_byte_flac=last_byte)

    # -- accessori comodi ---------------------------------------------------

    def cues(self) -> list["CueEntry"]:
        return [e for e in self.entries if isinstance(e, CueEntry)]

    def loops(self) -> list["LoopEntry"]:
        return [e for e in self.entries if isinstance(e, LoopEntry)]

    def is_bpm_locked(self) -> bool:
        return any(isinstance(e, BpmLockEntry) and e.locked for e in self.entries)


def _parse_entry(name: str, payload: bytes) -> Marker2Entry:
    """Decodifica una entry, conservando grezze quelle di tipo ignoto."""
    if name == "CUE":
        index = payload[1]
        position = struct.unpack_from(">I", payload, 2)[0]
        color = (payload[7], payload[8], payload[9])
        label = payload[12:].split(b"\x00")[0].decode("utf-8", "replace")
        return CueEntry(index=index, position_ms=position, color=color, label=label)
    if name == "LOOP":
        index = payload[1]
        start, end = struct.unpack_from(">II", payload, 2)
        color = (payload[15], payload[16], payload[17])
        locked = bool(payload[19])
        label = payload[20:].split(b"\x00")[0].decode("utf-8", "replace")
        return LoopEntry(index=index, start_ms=start, end_ms=end,
                         color=color, locked=locked, label=label)
    if name == "BPMLOCK":
        return BpmLockEntry(locked=bool(payload[0]))
    if name == "COLOR":
        return ColorEntry(color=(payload[1], payload[2], payload[3]))
    return UnknownEntry(raw_name=name, payload=payload)

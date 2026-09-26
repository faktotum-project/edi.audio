"""Da struttura del brano a hot cue e loop.

Regola che governa tutto il modulo: **ogni posizione viene agganciata al beat
più vicino**, e i loop a un confine di battuta. Un cue fuori griglia costringe
a correggere a mano proprio nel momento in cui non c'è tempo, ed è peggio di
un cue assente. Per lo stesso motivo, quando una sezione non si identifica con
confidenza sufficiente il cue si omette invece di essere piazzato a caso.

Disposizione prodotta:

    1  MIX IN    verde     primo downbeat utile
    2  DROP      rosso     salto di energia più marcato
    3  BREAK     blu       rientro di bassa energia dopo il drop
    4  MIX OUT   arancione inizio della coda
    5  →INTRO    ciano     inizio del loop di intro
    6  →OUTRO    magenta   inizio del loop di outro
    +  due loop salvati di 16 battute su intro e outro

Perché i cue 5 e 6 duplicano l'inizio dei loop: Mixxx tiene loop e hot cue
nello stesso spazio di indici e all'import somma 8 all'indice dei loop
(`src/track/serato/tags.cpp:36-44`), quindi i loop salvati atterrano sugli
hot cue 13 e 14 — fuori dal primo banco di pad di un controller. I due cue
in più mettono gli stessi punti d'aggancio sui pad 5 e 6, dove la mano li
trova senza cambiare modalità.
"""
from dataclasses import dataclass

from . import serato

BEATS_PER_BAR = 4
LOOP_BARS = 16

# Quanto deve valere il salto di energia del drop, come frazione dell'intera
# escursione dinamica del brano. La soglia è relativa e non assoluta perché la
# musica vera è molto più compressa di quanto sembri: su «Glue» dei Bicep lo
# stacco fra intro e sezione principale è di appena 0.09 su una scala 0-1,
# mentre un brano costruito a tavolino ne fa 0.65. Una soglia fissa tarata sul
# secondo caso non troverebbe mai un drop nel primo.
_MIN_DROP_ENERGY_JUMP_RATIO = 0.20
# Sotto questo salto assoluto non è un drop comunque, per quanto piatto sia il
# resto del brano.
_MIN_DROP_ENERGY_JUMP_FLOOR = 0.04
# Un breakdown deve scendere sotto questa frazione dell'energia del drop.
_MAX_BREAK_ENERGY_RATIO = 0.72


@dataclass
class CuePoint:
    index: int
    position_secs: float
    label: str
    color: tuple[int, int, int]


@dataclass
class SavedLoop:
    index: int
    start_secs: float
    end_secs: float
    label: str


@dataclass
class CueLayout:
    cues: list[CuePoint]
    loops: list[SavedLoop]
    notes: list[str]

    def to_markers2(self, bpm_locked: bool = True) -> serato.Markers2:
        entries: list[serato.Marker2Entry] = []
        if bpm_locked:
            entries.append(serato.BpmLockEntry(locked=True))
        for cue in self.cues:
            entries.append(serato.CueEntry(
                index=cue.index,
                position_ms=int(round(cue.position_secs * 1000)),
                color=cue.color,
                label=cue.label,
            ))
        for loop in self.loops:
            entries.append(serato.LoopEntry(
                index=loop.index,
                start_ms=int(round(loop.start_secs * 1000)),
                end_ms=int(round(loop.end_secs * 1000)),
                label=loop.label,
            ))
        return serato.Markers2(entries=entries)


def _snap(position: float, grid: list[float]) -> float | None:
    """Aggancia una posizione al valore più vicino della griglia."""
    if not grid:
        return None
    return min(grid, key=lambda t: abs(t - position))


def _next_downbeat(position: float, downbeats: list[float]) -> float | None:
    """Primo downbeat a partire da `position` compreso."""
    for t in downbeats:
        if t >= position - 1e-6:
            return t
    return downbeats[-1] if downbeats else None


def build(analysis: dict) -> CueLayout:
    """Costruisce la disposizione di cue e loop da un risultato di `analysis`.

    `notes` raccoglie i motivi per cui un cue è stato omesso: finiscono nel log
    e in UI, così è chiaro se una traccia va rifinita a mano.
    """
    notes: list[str] = []
    duration = float(analysis.get("duration") or 0.0)
    beats: list[float] = analysis.get("beat_times") or []
    downbeats: list[float] = analysis.get("downbeat_times") or []
    sections = (analysis.get("structure") or {}).get("sections") or []
    bpm = float(analysis.get("bpm") or 0.0)

    if not beats or not downbeats or not bpm:
        return CueLayout(cues=[], loops=[], notes=["griglia assente: nessun cue generato"])

    bar_secs = (60.0 / bpm) * BEATS_PER_BAR
    cues: list[CuePoint] = []
    loops: list[SavedLoop] = []

    # -- 1. MIX IN ---------------------------------------------------------
    first_sound = float(analysis.get("first_sound") or 0.0)
    mix_in = _next_downbeat(first_sound, downbeats)
    if mix_in is None:
        return CueLayout(cues=[], loops=[], notes=["nessun downbeat utile"])
    cues.append(CuePoint(0, mix_in, "MIX IN", serato.COLOR_GREEN))

    # -- 2. DROP -----------------------------------------------------------
    # Si cerca il passaggio fra sezioni adiacenti con il salto di energia più
    # grande, limitandosi alla parte centrale: un salto nei primi secondi è
    # l'ingresso del brano, non il drop.
    drop = None
    drop_section = None
    if len(sections) >= 2:
        energies = [s["energy"] for s in sections]
        dynamic_range = max(energies) - min(energies)
        threshold = max(_MIN_DROP_ENERGY_JUMP_FLOOR,
                        dynamic_range * _MIN_DROP_ENERGY_JUMP_RATIO)

        window = (duration * 0.10, duration * 0.85)
        best_jump = 0.0
        for prev, cur in zip(sections, sections[1:]):
            if not (window[0] <= cur["start"] <= window[1]):
                continue
            jump = cur["energy"] - prev["energy"]
            if jump > best_jump:
                best_jump, drop_section = jump, cur
        if drop_section is not None and best_jump < threshold:
            notes.append(f"drop non marcato (salto {best_jump:.2f} "
                         f"contro {threshold:.2f} richiesti): cue 2 omesso")
            drop_section = None
    else:
        notes.append("struttura troppo piatta per individuare il drop")

    if drop_section is not None:
        drop = _next_downbeat(drop_section["start"], downbeats)
        cues.append(CuePoint(1, drop, "DROP", serato.COLOR_RED))

    # -- 3. BREAK ----------------------------------------------------------
    brk = None
    # Sezione a bassa energia dopo il drop: è il punto in cui conviene entrare
    # con la traccia successiva.
    if drop_section is not None:
        # Il breakdown è la *prima* sezione tranquilla dopo il drop, non la più
        # tranquilla in assoluto: quella è quasi sempre la coda del brano.
        threshold = drop_section["energy"] * _MAX_BREAK_ENERGY_RATIO
        brk_section = next(
            (s for s in sections
             if s["start"] > drop_section["start"] + bar_secs and s["energy"] <= threshold),
            None,
        )
        if brk_section is not None:
            brk = _next_downbeat(brk_section["start"], downbeats)
        else:
            notes.append("nessun breakdown netto dopo il drop: cue 3 omesso")

    # -- 4. MIX OUT --------------------------------------------------------
    # Inizio della coda: l'ultima sezione a bassa energia. Se la traccia finisce
    # di colpo si ripiega su 32 battute prima della fine.
    mix_out = None
    if sections:
        tail = [s for s in sections if s["start"] > duration * 0.55]
        if tail:
            reference = max(s["energy"] for s in sections)
            quiet_tail = [s for s in tail if s["energy"] < reference * 0.65]
            if quiet_tail:
                mix_out = quiet_tail[-1]["start"]
    if mix_out is None:
        fallback = duration - bar_secs * 8
        if fallback > mix_in:
            mix_out = fallback
            notes.append("outro non riconosciuta: mix out a 32 battute dalla fine")
    if mix_out is not None:
        mix_out = _snap(mix_out, downbeats)
        if mix_out is None or mix_out <= mix_in + bar_secs:
            mix_out = None

    # -- Coerenza dell'ordine ---------------------------------------------
    # I quattro cue sono un percorso attraverso il brano e devono susseguirsi:
    # un BREAK che cade dopo il MIX OUT non è un errore di pochi millisecondi,
    # significa che le due ricerche hanno agganciato la stessa sezione. In quel
    # caso si tiene il punto di uscita, che serve a mixare, e si rinuncia al
    # break, che è solo un riferimento.
    if brk is not None and mix_out is not None and brk >= mix_out - bar_secs:
        notes.append("break e mix out coincidono: cue 3 omesso")
        brk = None
    if brk is not None and drop is not None and brk <= drop:
        notes.append("break individuato prima del drop: cue 3 omesso")
        brk = None

    if brk is not None:
        cues.append(CuePoint(2, brk, "BREAK", serato.COLOR_BLUE))
    if mix_out is not None:
        cues.append(CuePoint(3, mix_out, "MIX OUT", serato.COLOR_ORANGE))
    cues.sort(key=lambda c: c.index)

    # -- 5/6. Loop di intro e outro ---------------------------------------
    loop_len = bar_secs * LOOP_BARS
    intro_end = mix_in + loop_len
    if intro_end < duration:
        loops.append(SavedLoop(4, mix_in, _snap(intro_end, downbeats) or intro_end,
                               f"INTRO {LOOP_BARS}"))
        cues.append(CuePoint(4, mix_in, "→INTRO", serato.COLOR_CYAN))
    else:
        notes.append("brano troppo corto per il loop di intro")

    if mix_out is not None:
        outro_start = mix_out - loop_len
        if outro_start > mix_in:
            outro_start = _snap(outro_start, downbeats) or outro_start
            loops.append(SavedLoop(5, outro_start, mix_out, f"OUTRO {LOOP_BARS}"))
            cues.append(CuePoint(5, outro_start, "→OUTRO", serato.COLOR_MAGENTA))
        else:
            notes.append("intro e outro si sovrappongono: loop di outro omesso")

    cues.sort(key=lambda c: c.index)
    return CueLayout(cues=cues, loops=loops, notes=notes)

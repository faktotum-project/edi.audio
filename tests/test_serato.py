"""Valida gli encoder Serato contro i blob reali prodotti da Serato DJ.

Le fixture in `mixxx-main/src/test/serato/data/` sono i byte che Serato ha
davvero scritto nei file e che il parser di Mixxx sa leggere. Se il nostro
encoder li riproduce identici, Mixxx leggerà anche quello che scriviamo noi.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dj import serato

DATA = Path(__file__).resolve().parents[1] / "mixxx-main" / "src" / "test" / "serato" / "data"


def _fixtures(subdir: str):
    for file_type in ("mp3", "flac", "mp4", "ogg"):
        d = DATA / file_type / subdir
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.octet-stream")):
            yield file_type, path


def check_beatgrid():
    fails = []
    count = 0
    for file_type, path in _fixtures("beatgrid"):
        raw = path.read_bytes()
        try:
            grid = serato.BeatGrid.parse(raw, file_type)
            out = grid.dump(file_type)
        except Exception as exc:
            fails.append(f"{file_type}/{path.name}: eccezione {exc!r}")
            continue
        count += 1
        if out != raw:
            fails.append(f"{file_type}/{path.name}: round-trip diverso\n"
                         f"  atteso {raw[:64]!r}\n  ottenuto {out[:64]!r}")
    return count, fails


def check_markers2():
    fails = []
    count = 0
    for file_type, path in _fixtures("markers2"):
        raw = path.read_bytes()
        try:
            markers = serato.Markers2.parse(raw, file_type)
            out = markers.dump(file_type)
        except Exception as exc:
            fails.append(f"{file_type}/{path.name}: eccezione {exc!r}")
            continue
        count += 1
        if out != raw:
            fails.append(f"{file_type}/{path.name}: round-trip diverso\n"
                         f"  atteso   {raw[:80]!r}\n  ottenuto {out[:80]!r}")
    return count, fails


def check_fresh_write():
    """Costruisce tag da zero e li rilegge: è il caso che ci interessa davvero,
    perché è quello che la pipeline produrrà per ogni traccia scaricata."""
    fails = []

    grid = serato.BeatGrid.constant_tempo(first_beat_secs=0.482, bpm=128.0)
    markers = serato.Markers2(entries=[
        serato.BpmLockEntry(locked=True),
        serato.CueEntry(0, 482, serato.COLOR_GREEN, "MIX IN"),
        serato.CueEntry(1, 60482, serato.COLOR_RED, "DROP"),
        serato.CueEntry(2, 120482, serato.COLOR_BLUE, "BREAK"),
        serato.CueEntry(3, 180482, serato.COLOR_ORANGE, "MIX OUT"),
        serato.LoopEntry(4, 482, 30482, label="INTRO 16"),
        serato.LoopEntry(5, 150482, 180482, label="OUTRO 16"),
    ])

    for file_type in (serato.FLAC, serato.MP3, serato.MP4, serato.OGG):
        if file_type != serato.OGG:
            back = serato.BeatGrid.parse(grid.dump(file_type), file_type)
            if abs(back.markers[-1].bpm - 128.0) > 1e-3:
                fails.append(f"{file_type}: bpm {back.markers[-1].bpm} != 128.0")
            if abs(back.markers[-1].position_secs - 0.482) > 1e-6:
                fails.append(f"{file_type}: primo beat {back.markers[-1].position_secs} != 0.482")

        back = serato.Markers2.parse(markers.dump(file_type), file_type)
        if not back.is_bpm_locked():
            fails.append(f"{file_type}: BPMLOCK perso")
        cues = back.cues()
        if len(cues) != 4:
            fails.append(f"{file_type}: {len(cues)} cue invece di 4")
        elif [c.position_ms for c in cues] != [482, 60482, 120482, 180482]:
            fails.append(f"{file_type}: posizioni cue {[c.position_ms for c in cues]}")
        elif [c.label for c in cues] != ["MIX IN", "DROP", "BREAK", "MIX OUT"]:
            fails.append(f"{file_type}: etichette {[c.label for c in cues]}")
        elif cues[0].color != serato.COLOR_GREEN:
            fails.append(f"{file_type}: colore cue 1 {cues[0].color}")
        loops = back.loops()
        if len(loops) != 2:
            fails.append(f"{file_type}: {len(loops)} loop invece di 2")
        elif [(l.start_ms, l.end_ms) for l in loops] != [(482, 30482), (150482, 180482)]:
            fails.append(f"{file_type}: estremi loop {[(l.start_ms, l.end_ms) for l in loops]}")

    # Un beat ogni 60/128 s: verifica che la griglia si espanda col passo giusto
    beats = grid.beat_positions_secs(10.0)
    step = beats[1] - beats[0]
    if abs(step - 60.0 / 128.0) > 1e-6:
        fails.append(f"passo della griglia {step} != {60.0 / 128.0}")

    return 4, fails


if __name__ == "__main__":
    total_fails = []
    for label, fn in (("beatgrid", check_beatgrid),
                      ("markers2", check_markers2),
                      ("scrittura da zero", check_fresh_write)):
        count, fails = fn()
        status = "OK" if not fails else f"{len(fails)} FALLITI"
        print(f"{label}: {count} fixture verificate — {status}")
        for f in fails:
            print(f"  {f}")
        total_fails += fails
    sys.exit(1 if total_fails else 0)

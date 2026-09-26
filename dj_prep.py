#!/usr/bin/env python3
"""CLI della pipeline DJ di edi.audio.

    dj_prep.py prepare "URL o titolo"   scarica la sorgente migliore e prepara
    dj_prep.py rescan  ~/Musica/DJ      prepara i FLAC già presenti
    dj_prep.py export  ~/Musica/DJ      rigenera playlist e rekordbox.xml
    dj_prep.py inspect traccia.flac     rilegge griglia e cue dal file
"""
import argparse
import sys
from pathlib import Path

from dj import analysis, export, mixxx, pipeline, tags

DEFAULT_DIR = str(Path.home() / "Musica" / "DJ")


def _progress(phase, message=""):
    print(f"  [{phase}] {message}".rstrip(), flush=True)


def _print_result(result) -> None:
    if not result.ok:
        print(f"  ✗ {result.error}", file=sys.stderr)
        return
    print(f"  ✓ {Path(result.path).name}")
    print(f"    {result.summary()}")
    for note in result.notes:
        print(f"    · {note}")


def cmd_prepare(args) -> int:
    if not analysis.is_available():
        print("ATTENZIONE: librosa non disponibile — esegui ./setup_analysis_venv.sh\n"
              "Il download funziona comunque, ma senza BPM, chiave e cue.\n", file=sys.stderr)

    queries = pipeline.expand_queries(list(args.query), progress=_progress)
    results = pipeline.prepare_many(
        queries, args.output, progress=_progress,
        workers=args.workers,
        search_alternatives=not args.no_search,
        allow_variants=args.allow_variants,
    )
    print()
    for result in results:
        _print_result(result)

    failures = sum(1 for r in results if not r.ok)
    print(f"\n{len(results) - failures}/{len(results)} tracce pronte")

    if args.export:
        info = export.export_all(args.output, organize=args.organize)
        _print_export(info)
    return 1 if failures else 0


def _print_export(info) -> None:
    print(f"\n{info['tracks']} tracce indicizzate")
    print(f"  {len(info['playlists'])} playlist · {len(info['crates'])} crate · rekordbox.xml")
    if info.get("organized"):
        print("  file riorganizzati in cartelle per genere")
    print("  In Mixxx: tasto destro su Crates → Import Crate, e seleziona i file in crates/")


def cmd_rescan(args) -> int:
    results = pipeline.rescan(args.directory, progress=_progress,
                              rename=not args.no_rename, workers=args.workers,
                              force=args.force)
    if not results:
        print("Nessuna traccia da preparare: sono già tutte pronte.")
    else:
        print()
        for result in results:
            _print_result(result)
        ok = sum(1 for r in results if r.ok)
        print(f"\n{ok}/{len(results)} tracce preparate")

    if args.export:
        _print_export(export.export_all(args.directory, organize=args.organize))
    return 0


def cmd_export(args) -> int:
    _print_export(export.export_all(args.directory, organize=args.organize))
    return 0


def cmd_doctor(args) -> int:
    """Verifica che l'ambiente sia in grado di fare tutto quello che promette."""
    import shutil

    ok = True

    def check(label, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print(f"  {'✓' if passed else '✗'}  {label}" + (f" — {detail}" if detail else ""))

    print("Strumenti")
    check("ffmpeg", shutil.which("ffmpeg") is not None, "serve per scaricare e convertire")
    check("analisi audio", analysis.is_available(),
          str(analysis.analysis_python() or "esegui ./setup_analysis_venv.sh"))
    print(f"  ·  {pipeline.default_workers()} tracce analizzate in parallelo")

    print("\nMixxx")
    status = mixxx.roundtrip_status()
    check("installato", status["installed"], "sudo apt install mixxx")
    check("configurazione trovata", status["config"] is not None, status["config"] or "")
    check("restituisce le correzioni fatte a mano", status["ok"], status.get("reason", ""))
    for step in status.get("steps", []):
        print(f"     → {step}")

    if not status["ok"]:
        print("\n  Senza queste due opzioni puoi comunque correggere in Mixxx, ma le")
        print("  correzioni restano nel suo database: edi.audio non le vedrà e una")
        print("  ri-analisi le sovrascriverebbe.")

    return 0 if ok else 1


def cmd_inspect(args) -> int:
    """Rilegge dal file quello che Mixxx leggerà. È la verifica che va superata
    prima ancora di aprire Mixxx: se qui i valori non tornano, non torneranno
    nemmeno là."""
    info = tags.read(args.path)
    print(f"{info['artist']} — {info['title']}")
    print(f"  durata      {info['duration']:.1f} s  "
          f"{info['sample_rate']} Hz  {info['bits_per_sample']} bit")
    print(f"  bpm         {info['bpm']}")
    print(f"  chiave      {info['key']}")
    print(f"  replaygain  {info['replay_gain']}")
    print(f"  commento    {info['comment']}")
    print(f"  copertina   {'sì' if info['has_cover'] else 'no'}")
    print(f"  bpm bloccato{'  sì' if info['bpm_locked'] else '  no'}")

    grid = info["beatgrid"]
    if grid:
        print(f"  griglia     {grid['markers']} marca/marche, primo beat "
              f"{grid['first_beat']} s, {grid['bpm']} BPM")
    else:
        print("  griglia     assente")

    if info["cues"]:
        print("  hot cue:")
        for cue in info["cues"]:
            secs = cue["position_ms"] / 1000
            print(f"    {cue['index']}  {secs // 60:02.0f}:{secs % 60:06.3f}  "
                  f"{cue['label']:10s} {cue['color']}")
    if info["loops"]:
        print("  loop salvati:")
        for loop in info["loops"]:
            print(f"    {loop['index']}  {loop['start_ms'] / 1000:8.3f} → "
                  f"{loop['end_ms'] / 1000:8.3f} s  {loop['label']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dj_prep",
        description="Prepara tracce pronte per il mixaggio in Mixxx.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_export_flags(parser_):
        parser_.add_argument("--export", action="store_true",
                             help="rigenera playlist, crate e XML alla fine")
        parser_.add_argument("--organize", action="store_true",
                             help="sposta i file in sottocartelle per genere")

    p = sub.add_parser("prepare", help="scarica la sorgente migliore e prepara la traccia")
    p.add_argument("query", nargs="+",
                   help="URL, link di playlist, oppure «artista - titolo»")
    p.add_argument("--output", default=DEFAULT_DIR, help=f"cartella (default: {DEFAULT_DIR})")
    p.add_argument("--no-search", action="store_true",
                   help="usa solo l'URL fornito, senza cercare sorgenti migliori")
    p.add_argument("--allow-variants", action="store_true",
                   help="non penalizzare live, remix, versioni accelerate")
    p.add_argument("--workers", type=int, default=None,
                   help="quante tracce preparare insieme (default: automatico)")
    add_export_flags(p)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("rescan", help="prepara i FLAC già presenti in una cartella")
    p.add_argument("directory")
    p.add_argument("--no-rename", action="store_true", help="non rinominare i file")
    p.add_argument("--workers", type=int, default=None,
                   help="quante tracce analizzare insieme (default: automatico)")
    p.add_argument("--force", action="store_true",
                   help="riscrivi griglia e cue anche dove sono stati corretti a mano")
    add_export_flags(p)
    p.set_defaults(func=cmd_rescan)

    p = sub.add_parser("export", help="rigenera playlist, crate e rekordbox.xml")
    p.add_argument("directory")
    p.add_argument("--organize", action="store_true",
                   help="sposta i file in sottocartelle per genere")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("doctor", help="verifica ambiente e configurazione di Mixxx")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("inspect", help="mostra griglia e cue scritti in un file")
    p.add_argument("path")
    p.set_defaults(func=cmd_inspect)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

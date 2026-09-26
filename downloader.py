#!/usr/bin/env python3
"""CLI di solo download, senza preparazione DJ.

Delega la logica a core.py e supporta sia URL web sia link Spotify. Per
ottenere tracce già analizzate, con griglia e hot cue, usare `dj_prep.py`.
"""
import argparse
import os
import sys

import core


def _ytdlp_hook(d):
    if d["status"] == "downloading":
        pct  = d.get("_percent_str", "?").strip()
        spd  = d.get("_speed_str", "").strip()
        name = os.path.basename(d.get("filename", "")).rsplit(".", 1)[0]
        print(f"\r  {name}  {pct}  {spd}        ", end="", flush=True)
    elif d["status"] == "finished":
        print()


def _spotdl_line(line):
    if any(x in line for x in ("[debug]", "WARNING:", "[ffmpeg]")):
        return
    print(f"  {line}")


def main():
    parser = argparse.ArgumentParser(
        description="DJ Downloader — scarica tracce per DJ set (WAV/MP3)."
    )
    parser.add_argument("url", help="URL della canzone o playlist (anche Spotify)")
    parser.add_argument("--format", choices=["flac", "mp3", "wav"], default=core.DEFAULT_FORMAT,
                        help=f"Formato audio (default: {core.DEFAULT_FORMAT}). "
                             "Il WAV non può contenere beatgrid e cue leggibili da Mixxx.")
    parser.add_argument("--output", default="./downloads",
                        help="Cartella di destinazione (default: ./downloads)")
    parser.add_argument("--quality", type=int, default=320,
                        help="Bitrate MP3 in kbps, ignorato per WAV (default: 320)")
    args = parser.parse_args()

    if not core.check_ffmpeg():
        print("ERRORE: ffmpeg non trovato nel PATH.", file=sys.stderr)
        print("  Linux:  sudo apt install ffmpeg", file=sys.stderr)
        print("  macOS:  brew install ffmpeg", file=sys.stderr)
        sys.exit(1)

    if args.format == "wav":
        print("Nota: in WAV non si possono scrivere beatgrid e hot cue.\n"
              "      Per una traccia pronta al mix usa: dj_prep.py prepare\n", file=sys.stderr)

    source = "Spotify" if core.is_spotify_url(args.url) else "Web"
    fmt_str = args.format.upper()
    qual_str = f"  {args.quality} kbps" if args.format == "mp3" else ""
    print(f"Sorgente:     {source}")
    print(f"Formato:      {fmt_str}{qual_str}")
    print(f"Destinazione: {os.path.abspath(args.output)}")
    print()

    try:
        core.download_one(
            args.url, args.format, args.output, args.quality,
            progress_hook=_ytdlp_hook,
            on_line=_spotdl_line,
        )
    except Exception as exc:
        print(f"\nERRORE: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nDownload completato.")


if __name__ == "__main__":
    main()

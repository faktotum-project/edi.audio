#!/usr/bin/env python3
"""Logica condivisa di download e cronologia.

Supporta:
  - YouTube, SoundCloud, Bandcamp e ~1800 sorgenti via yt-dlp
  - Brani e playlist Spotify via spotdl

Usato dalla CLI (downloader.py) e dalla GUI (app.py).
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yt_dlp

HISTORY_DIR = Path.home() / ".dj_downloader"
HISTORY_FILE = HISTORY_DIR / "history.json"

# Formati che possono contenere i tag Serato letti da Mixxx: il WAV non è fra
# questi, quindi non è più un'opzione per il flusso DJ.
DJ_READY_FORMATS = ("flac", "mp3", "aiff")
DEFAULT_FORMAT = "flac"


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def check_ffmpeg():
    """True se ffmpeg è disponibile nel PATH."""
    return shutil.which("ffmpeg") is not None


def is_spotify_url(url):
    """True se l'URL è un link Spotify (brano, album, playlist)."""
    return "open.spotify.com" in url or "spotify.link" in url


def _clean_env():
    """Copia dell'env senza variabili proxy (per subprocess diretti)."""
    env = os.environ.copy()
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(var, None)
    return env


# ---------------------------------------------------------------------------
# yt-dlp — YouTube, SoundCloud, Bandcamp, …
# ---------------------------------------------------------------------------

def build_opts(fmt, output_dir, quality=320, progress_hook=None):
    """Opzioni yt-dlp per estrarre audio con metadata e copertina.

    FLAC è il formato di riferimento per il flusso DJ: è l'unico fra quelli
    non compressi che Mixxx sa leggere insieme ai tag Serato con beatgrid e
    hot cue. Il WAV non li supporta affatto (`trackmetadata_riff.cpp` non ha
    alcun codice Serato), e in MP3 le posizioni dei cue vanno corrette con un
    offset che dipende dal decoder (`src/track/serato/tags.cpp:86`), mentre in
    FLAC sono esatte al campione.

    EmbedThumbnail viene omesso per WAV perché yt-dlp non supporta
    l'embedding di copertine in file WAV e lancerebbe un errore nel
    postprocessor.
    """
    os.makedirs(output_dir, exist_ok=True)

    # %(playlist_index&…|)s: prefisso numerico solo se l'URL è una playlist
    # Usa %{playlist_index} per riferimento annidato (sintassi yt-dlp)
    outtmpl = os.path.join(
        output_dir,
        "%(playlist_index&%{playlist_index}03d - |)s%(title)s.%(ext)s",
    )

    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
            **({"preferredquality": str(quality)} if fmt == "mp3" else {}),
        },
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if fmt != "wav":
        postprocessors.append({"key": "EmbedThumbnail"})

    opts = {
        "format": "bestaudio/best",
        "postprocessors": postprocessors,
        "outtmpl": outtmpl,
        # thumbnail viene salvato solo per MP3 (poi EmbedThumbnail lo incorpora)
        "writethumbnail": fmt != "wav",
        # proxy="" sovrascrive HTTPS_PROXY env; su macchine senza proxy non cambia nulla
        "proxy": "",
        # ignoreerrors=True: per le playlist un brano fallito non blocca il resto
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": False,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _download_ytdlp(url, fmt, output_dir, quality, progress_hook, record_history):
    opts = build_opts(fmt, output_dir, quality, progress_hook)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if info is None:
        raise RuntimeError("Download fallito: impossibile recuperare informazioni per questo URL.")

    # Per playlist info["entries"] contiene i singoli brani
    title = info.get("title") or info.get("id") or url

    if record_history:
        add_to_history({
            "url": url,
            "title": title,
            "format": fmt,
            "path": output_dir,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "ytdlp",
        })
    return {"title": title, "format": fmt, "path": output_dir}


# ---------------------------------------------------------------------------
# spotdl — Spotify
# ---------------------------------------------------------------------------

def _download_spotify(url, fmt, output_dir, quality, on_line):
    """Scarica un URL Spotify via spotdl (subprocess).

    on_line(str): callback chiamato per ogni riga di output (per la GUI).
    """
    os.makedirs(output_dir, exist_ok=True)

    # Template di output spotdl: "Artista - Titolo.ext"
    out_template = os.path.join(output_dir, "{artists} - {title}")

    cmd = [
        sys.executable, "-m", "spotdl", "download", url,
        "--output", out_template,
        "--format", fmt,
        "--preload",
    ]
    if fmt == "mp3":
        cmd += ["--bitrate", f"{quality}k"]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_clean_env(),
    )

    last_lines = []
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        last_lines.append(line)
        if len(last_lines) > 10:
            last_lines.pop(0)
        if on_line:
            on_line(line)

    proc.wait()
    if proc.returncode != 0:
        excerpt = "\n".join(last_lines[-5:])
        raise RuntimeError(f"spotdl ha restituito errore {proc.returncode}:\n{excerpt}")

    return {"format": fmt, "path": output_dir}


# ---------------------------------------------------------------------------
# Entrypoint unificato
# ---------------------------------------------------------------------------

def download_one(url, fmt=DEFAULT_FORMAT, output_dir="./downloads", quality=320,
                 progress_hook=None, on_line=None, record_history=True):
    """Scarica un URL (brano o playlist) nel formato richiesto.

    Parametri
    ---------
    progress_hook : callable   — progress hook yt-dlp (% per byte, per yt-dlp)
    on_line       : callable   — callback per ogni riga output spotdl
    """
    if is_spotify_url(url):
        result = _download_spotify(url, fmt, output_dir, quality, on_line)
        title = url.rstrip("/").split("/")[-1]
        if record_history:
            add_to_history({
                "url": url,
                "title": title,
                "format": fmt,
                "path": output_dir,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "source": "spotify",
            })
        result["title"] = title
        return result
    else:
        return _download_ytdlp(url, fmt, output_dir, quality, progress_hook, record_history)


# ---------------------------------------------------------------------------
# Cronologia
# ---------------------------------------------------------------------------

def load_history():
    """Carica la cronologia. Ritorna [] se assente o corrotta."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def add_to_history(entry):
    """Aggiunge una entry alla cronologia su disco."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    history = load_history()
    history.append(entry)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def is_downloaded(url):
    """True se l'URL è già nella cronologia."""
    return any(e.get("url") == url for e in load_history())

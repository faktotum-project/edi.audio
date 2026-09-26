#!/usr/bin/env bash
# Installa le dipendenze in un virtualenv locale e avvia la GUI.
# Uso:  ./run.sh        (avvia l'app)
#       ./run.sh cli ... (passa gli argomenti a downloader.py)
set -e
cd "$(dirname "$0")"

# Crea il virtualenv al primo avvio
if [ ! -d ".venv" ]; then
    echo "Creo il virtualenv (.venv)..."
    python3 -m venv .venv
    .venv/bin/python -m ensurepip --upgrade
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
fi

# Avviso se ffmpeg manca
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "ATTENZIONE: ffmpeg non trovato nel PATH."
    echo "  Debian/Ubuntu: sudo apt install ffmpeg"
    echo "  macOS:         brew install ffmpeg"
fi

if [ "$1" = "cli" ]; then
    shift
    exec .venv/bin/python downloader.py "$@"
else
    exec .venv/bin/python app.py
fi

#!/usr/bin/env bash
# =====================================================================
#  DJ Downloader — Installazione automatica per Linux
#  Installa: ffmpeg, python3-venv, python3-tk + dipendenze Python.
#  Uso:  ./installa.sh
# =====================================================================
set -e
cd "$(dirname "$0")"

echo
echo "  ====================================================="
echo "   DJ Downloader — Installazione automatica (Linux)"
echo "  ====================================================="
echo

# --- Rileva il gestore di pacchetti -----------------------------------
detect_pm() {
    if command -v apt-get >/dev/null 2>&1; then echo "apt"; return; fi
    if command -v dnf      >/dev/null 2>&1; then echo "dnf"; return; fi
    if command -v pacman   >/dev/null 2>&1; then echo "pacman"; return; fi
    if command -v zypper   >/dev/null 2>&1; then echo "zypper"; return; fi
    echo "unknown"
}
PM=$(detect_pm)

# Sudo solo se non siamo root
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

echo "  [1/3] Installo i pacchetti di sistema (ffmpeg, python3-venv, tkinter)..."
case "$PM" in
    apt)
        $SUDO apt-get update -qq || true
        $SUDO apt-get install -y ffmpeg python3-venv python3-tk python3-pip
        ;;
    dnf)
        $SUDO dnf install -y ffmpeg python3-virtualenv python3-tkinter python3-pip \
            || { echo "  (Se ffmpeg manca, abilita RPM Fusion: https://rpmfusion.org)"; }
        ;;
    pacman)
        $SUDO pacman -Sy --noconfirm ffmpeg python tk python-pip
        ;;
    zypper)
        $SUDO zypper install -y ffmpeg python3-venv python3-tk python3-pip
        ;;
    *)
        echo "  Gestore di pacchetti non riconosciuto."
        echo "  Installa manualmente: ffmpeg, python3-venv, python3-tk (tkinter)."
        ;;
esac

# --- Crea il virtualenv -----------------------------------------------
echo "  [2/3] Creo l'ambiente virtuale Python (.venv)..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/python -m ensurepip --upgrade
fi

# --- Installa le dipendenze Python ------------------------------------
echo "  [3/3] Installo le dipendenze Python (yt-dlp, spotdl)..."
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt --quiet

echo
echo "  ====================================================="
echo "   Installazione completata!"
echo "  ====================================================="
echo
echo "  Per avviare l'app:   ./run.sh"
echo "  (oppure doppio clic su 'DJ-Downloader.desktop')"
echo

# Avvia subito l'app se siamo in una sessione grafica
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    echo "  Avvio l'app ora..."
    exec .venv/bin/python app.py
fi

#!/usr/bin/env bash
# Installa lo stack di analisi audio (librosa) usato per BPM, chiave e cue.
#
# Da librosa 1.0 l'analisi gira anche su Python 3.14, quindi di norma basta
# installarla nel venv dell'app. Solo se lì non si installa si ripiega su un
# interprete separato, che la pipeline userà al posto di quello corrente.
set -euo pipefail

cd "$(dirname "$0")"
MAIN_VENV=".venv"
FALLBACK_VENV=".venv-analysis"
PACKAGES=("librosa>=1.0" "numpy>=1.24" "scipy>=1.10" "soundfile>=0.12")

try_install() {
    local python="$1"
    "$python" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$python" -m pip install "${PACKAGES[@]}" && "$python" -c "import librosa" 2>/dev/null
}

if [ -x "$MAIN_VENV/bin/python" ]; then
    echo "Installo librosa nel venv dell'app…"
    if try_install "$MAIN_VENV/bin/python"; then
        echo
        "$MAIN_VENV/bin/python" -c "import librosa; print('  librosa', librosa.__version__, '— pronto')"
        exit 0
    fi
    echo "Installazione nel venv principale fallita, provo con un interprete separato." >&2
fi

find_python() {
    for candidate in python3.12 python3.11; do
        command -v "$candidate" >/dev/null 2>&1 && { echo "$candidate"; return 0; }
    done
    if command -v uv >/dev/null 2>&1; then
        uv python install 3.12 >/dev/null 2>&1 || true
        uv python find 3.12 2>/dev/null && return 0
    fi
    return 1
}

PY="$(find_python)" || {
    cat >&2 <<'MSG'
ERRORE: non riesco a installare librosa.

Prova con un interprete più vecchio:
  sudo apt install python3.12 python3.12-venv
oppure installa uv, che se lo scarica da solo:
  curl -LsSf https://astral.sh/uv/install.sh | sh
MSG
    exit 1
}

echo "Creo un venv di analisi separato con $PY"
[ -x "$FALLBACK_VENV/bin/python" ] || "$PY" -m venv "$FALLBACK_VENV"
try_install "$FALLBACK_VENV/bin/python"
echo
"$FALLBACK_VENV/bin/python" -c "import librosa; print('  librosa', librosa.__version__, '— pronto')"

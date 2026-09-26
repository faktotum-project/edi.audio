"""Ponte fra l'app e il worker che calcola BPM, chiave e struttura.

Il worker gira sempre come processo separato, anche quando l'interprete è lo
stesso: librosa carica parecchie decine di MB e tiene memoria in modo poco
prevedibile su file lunghi, e isolarlo significa che un'analisi che va male
non si porta dietro il server.

Da librosa 1.0 l'analisi gira anche su Python 3.14, quindi di norma basta
l'interprete dell'app. Il venv separato resta come ripiego per gli ambienti
dove librosa non si installa: se esiste, viene usato al posto dell'interprete
corrente.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WORKER = Path(__file__).resolve().parent / "analyzer_worker.py"

# Dove `setup_analysis_venv.sh` piazza l'interprete dedicato.
ANALYSIS_VENV = _ROOT / ".venv-analysis"


class AnalysisUnavailable(RuntimeError):
    """Il venv di analisi non c'è o non è utilizzabile."""


def _has_librosa(python: Path) -> bool:
    try:
        proc = subprocess.run([str(python), "-c", "import librosa"],
                              capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def analysis_python() -> Path | None:
    """Interprete da usare per l'analisi, o `None` se non se ne trova uno.

    Ordine: variabile d'ambiente, venv dedicato se presente, interprete
    corrente. `DJ_ANALYSIS_PYTHON` permette di puntare a un interprete già
    pronto senza creare nulla.
    """
    override = os.environ.get("DJ_ANALYSIS_PYTHON")
    if override and Path(override).exists():
        return Path(override)

    dedicated = ANALYSIS_VENV / "bin" / "python"
    if dedicated.exists():
        return dedicated

    current = Path(sys.executable)
    if _has_librosa(current):
        return current
    return None


# La verifica lancia un subprocess: si memorizza, perché la UI la interroga
# a ogni caricamento di pagina.
_available_cache: bool | None = None


def is_available() -> bool:
    global _available_cache
    if _available_cache is None:
        _available_cache = analysis_python() is not None
    return _available_cache


def analyze(path, timeout: int = 600) -> dict:
    """Analizza un file audio. Solleva `AnalysisUnavailable` se manca il venv."""
    python = analysis_python()
    if python is None:
        raise AnalysisUnavailable(
            "librosa non disponibile — esegui ./setup_analysis_venv.sh "
            "(oppure imposta DJ_ANALYSIS_PYTHON su un interprete che ce l'ha)"
        )

    # Un solo thread per processo: quando si analizzano più tracce in
    # parallelo, lasciare che ogni worker apra a sua volta un pool di thread
    # BLAS satura la macchina e rende il tutto più lento del seriale.
    env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1",
               MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1")

    proc = subprocess.run(
        [str(python), str(_WORKER), str(path)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"analisi fallita senza output: {proc.stderr.strip()[:300]}")
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"il worker non ha restituito JSON: {proc.stdout[:200]}")
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "analisi fallita"))
    return result


# ---------------------------------------------------------------------------
# ReplayGain — resta in ffmpeg, che lo calcola secondo EBU R128
# ---------------------------------------------------------------------------

_LOUDNESS_RE = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.MULTILINE)
_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?[\d.]+)\s*dBFS", re.MULTILINE)

# ReplayGain 2.0 allinea le tracce a -18 LUFS.
_TARGET_LUFS = -18.0


def replay_gain(path) -> dict | None:
    """Guadagno e picco secondo EBU R128, nel formato dei tag ReplayGain.

    Serve perché in un set le tracce devono partire tutte allo stesso volume
    percepito: senza, ogni cambio brano è una correzione di gain a mano.
    """
    if not shutil.which("ffmpeg"):
        return None

    proc = subprocess.run(
        ["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    output = proc.stderr

    loudness = _LOUDNESS_RE.findall(output)
    peaks = _PEAK_RE.findall(output)
    if not loudness:
        return None

    integrated = float(loudness[-1])
    gain_db = _TARGET_LUFS - integrated
    peak_dbfs = float(peaks[-1]) if peaks else 0.0
    # I tag ReplayGain vogliono il picco come rapporto lineare, non in dB.
    peak_ratio = 10 ** (peak_dbfs / 20.0)

    return {
        "gain_db": round(gain_db, 2),
        "peak": round(peak_ratio, 6),
        "loudness_lufs": round(integrated, 2),
    }


if __name__ == "__main__":
    # Utile per provare il ponte senza passare dalla pipeline.
    print(json.dumps(analyze(sys.argv[1]), indent=2, ensure_ascii=False))

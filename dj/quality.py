"""Misura della qualità *reale* di un file audio, non di quella dichiarata.

Il problema che questo modulo risolve: su YouTube e SoundCloud circolano
moltissimi upload che sono MP3 a 128 kbps ri-codificati e ripresentati come
"320 kbps" o "HQ". L'unico modo per accorgersene senza ascoltare è guardare
lo spettro: ogni codec lossy taglia via tutto sopra una certa frequenza, e
quel taglio resta impresso anche se il file viene poi ri-codificato meglio o
messo dentro un contenitore lossless.

Un FLAC ottenuto da una sorgente a 128 kbps ha ancora il muro a ~16 kHz. Se lo
troviamo, sappiamo che il file è lossless solo di nome.

Nessuna dipendenza da librosa: qui bastano ffmpeg e la FFT di numpy — e se
numpy non c'è, si ripiega su un'analisi in Python puro.
"""
import json
import math
import shutil
import struct
import subprocess
from dataclasses import dataclass, field

# Taglio tipico per bitrate, misurato su codifiche LAME/Opus/AAC reali.
# Serve a tradurre una frequenza in un giudizio comprensibile.
# Valori tarati misurando codifiche LAME reali dello stesso sorgente:
# 128 kbps taglia a ~16.5 kHz, 192 a ~19 kHz, 320 a ~20.5 kHz, il lossless
# non taglia affatto e arriva fino a Nyquist.
_CUTOFF_TIERS = [
    (21000, "nessun taglio"),
    (19800, "≈320 kbps"),
    (18300, "≈192-256 kbps"),
    (16000, "≈128-160 kbps"),
    (0, "sotto 128 kbps"),
]

# Codec senza perdita: se il contenitore è uno di questi, il taglio spettrale
# racconta la storia della *sorgente*, non della codifica finale.
_LOSSLESS_CODECS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "wavpack", "ape"}

# Durata della finestra analizzata, presa dal centro del brano dove la musica
# è più densa (intro e code ingannano la misura).
_PROBE_SECONDS = 20


@dataclass
class QualityReport:
    """Verdetto su un file. `score` è 0-100 e serve solo a confrontare candidati."""
    codec: str = ""
    bitrate_kbps: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    duration: float | None = None
    cutoff_hz: float | None = None
    tier: str = ""
    lossless_container: bool = False
    transcoded: bool = False
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def label(self) -> str:
        """Etichetta onesta da mostrare in UI.

        Un FLAC ricavato da una sorgente lossy non va chiamato "lossless": il
        contenitore non è una promessa sul contenuto.
        """
        if self.lossless_container and self.transcoded:
            # Si dichiara il taglio misurato, non un bitrate equivalente: le
            # soglie sono tarate su MP3, e un Opus a 136 kbps conserva fino a
            # 20 kHz, dove un MP3 arriva solo a 320. Stimare il bitrate della
            # sorgente senza sapere quale codec fosse significa sbagliarlo.
            cutoff = f"taglio {self.cutoff_hz / 1000:.1f} kHz" if self.cutoff_hz else self.tier
            return f"{self.codec.upper()} da sorgente lossy ({cutoff})"
        if self.lossless_container:
            return f"{self.codec.upper()} lossless"
        if self.bitrate_kbps:
            return f"{self.bitrate_kbps} kbps {self.codec.upper()}"
        return f"{self.codec.upper()} ({self.tier})"


def probe(path) -> dict:
    """Metadati del flusso audio via ffprobe."""
    if not shutil.which("ffprobe"):
        return {}
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", "-select_streams", "a:0", str(path)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    streams = data.get("streams") or [{}]
    fmt = data.get("format") or {}
    stream = streams[0]

    bitrate = stream.get("bit_rate") or fmt.get("bit_rate")
    return {
        "codec": stream.get("codec_name", ""),
        "sample_rate": int(stream["sample_rate"]) if stream.get("sample_rate") else None,
        "channels": stream.get("channels"),
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "bitrate_kbps": int(int(bitrate) / 1000) if bitrate else None,
    }


def _decode_window(path, start: float, seconds: int, sample_rate: int = 48000) -> bytes:
    """Estrae una finestra mono PCM 16 bit tramite ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", f"{start:.2f}", "-t", str(seconds),
         "-i", str(path), "-ac", "1", "-ar", str(sample_rate),
         "-f", "s16le", "-"],
        capture_output=True,
    )
    return proc.stdout


def measure_cutoff(path, duration: float | None = None,
                   sample_rate: int = 48000) -> float | None:
    """Frequenza sopra la quale lo spettro crolla, in Hz.

    Si media lo spettro su più finestre e si cerca il punto in cui l'energia
    scende stabilmente sotto una soglia rispetto alla banda medio-alta. Si
    guarda l'energia *cumulata* dall'alto verso il basso, perché un singolo bin
    rumoroso sopra il taglio non deve spostare il verdetto.
    """
    if not shutil.which("ffmpeg"):
        return None

    start = max(0.0, (duration or 60.0) / 2 - _PROBE_SECONDS / 2)
    raw = _decode_window(path, start, _PROBE_SECONDS, sample_rate)
    if len(raw) < sample_rate * 2:  # meno di un secondo di audio
        raw = _decode_window(path, 0.0, _PROBE_SECONDS, sample_rate)
    if len(raw) < sample_rate:
        return None

    samples = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])

    try:
        import numpy as np
    except ImportError:
        return _measure_cutoff_pure(samples, sample_rate)

    data = np.array(samples, dtype=np.float64) / 32768.0
    window_size = 8192
    frames = len(data) // window_size
    if frames < 2:
        return None

    window = np.hanning(window_size)
    accum = np.zeros(window_size // 2 + 1)
    used = 0
    for i in range(frames):
        chunk = data[i * window_size: (i + 1) * window_size]
        # Le finestre quasi mute falserebbero la media verso il basso
        if np.sqrt(np.mean(chunk ** 2)) < 0.005:
            continue
        accum += np.abs(np.fft.rfft(chunk * window)) ** 2
        used += 1
    if used == 0:
        return None

    spectrum = accum / used
    freqs = np.fft.rfftfreq(window_size, 1.0 / sample_rate)

    # Riferimento: energia media fra 1 e 6 kHz, dove c'è sempre segnale.
    reference = float(np.mean(spectrum[(freqs >= 1000) & (freqs <= 6000)]))
    if reference <= 0:
        return None

    return _cliff_frequency(freqs, spectrum, reference, sample_rate, np)


def _cliff_frequency(freqs, spectrum, reference, sample_rate, np) -> float | None:
    """Frequenza più alta la cui *banda* ha ancora segnale.

    Si ragiona per bande da 500 Hz invece che per singoli bin: sopra il taglio
    resta sempre qualche bin di rumore isolato, e prendere l'ultimo bin sopra
    soglia farebbe leggere ogni file come se non fosse tagliato affatto. Il
    dirupo vero è di 60-70 dB, quindi una soglia a -40 dB lo trova senza
    ambiguità.
    """
    threshold = reference * (10 ** (-40 / 10))
    nyquist = sample_rate / 2
    band = 500.0

    cutoff = None
    low = 6000.0
    while low < nyquist:
        high = min(low + band, nyquist)
        mask = (freqs >= low) & (freqs < high)
        if mask.any() and float(np.mean(spectrum[mask])) > threshold:
            cutoff = high
        low = high

    if cutoff is None:
        return 6000.0
    # A ridosso di Nyquist non c'è nessun taglio da segnalare: è il limite
    # della misura, non del file.
    if cutoff >= nyquist - band:
        return float(nyquist)
    return float(cutoff)


def _measure_cutoff_pure(samples, sample_rate: int) -> float | None:
    """Variante senza numpy: DFT di Goertzel su una griglia di frequenze.

    Molto più lenta e grossolana, ma evita che l'assenza di numpy renda muta
    la verifica di qualità, che è il punto centrale della pipeline.
    """
    size = 8192
    if len(samples) < size:
        return None
    chunk = [s / 32768.0 for s in samples[len(samples) // 2: len(samples) // 2 + size]]

    def band_energy(freq: float) -> float:
        k = freq * size / sample_rate
        omega = 2 * math.pi * k / size
        coeff = 2 * math.cos(omega)
        s_prev = s_prev2 = 0.0
        for n, sample in enumerate(chunk):
            s = sample * (0.5 - 0.5 * math.cos(2 * math.pi * n / size)) + coeff * s_prev - s_prev2
            s_prev2, s_prev = s_prev, s
        return s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2

    reference = max(band_energy(f) for f in (1000, 2000, 4000, 6000))
    if reference <= 0:
        return None
    threshold = reference * (10 ** (-50 / 10))

    cutoff = 6000.0
    for freq in range(7000, min(22000, sample_rate // 2), 500):
        if band_energy(float(freq)) > threshold:
            cutoff = float(freq)
    return cutoff


def _tier_for(cutoff: float | None) -> str:
    if cutoff is None:
        return "non misurato"
    for limit, label in _CUTOFF_TIERS:
        if cutoff >= limit:
            return label
    return "sconosciuto"


def assess(path) -> QualityReport:
    """Verdetto completo su un file."""
    info = probe(path)
    report = QualityReport(
        codec=info.get("codec", ""),
        bitrate_kbps=info.get("bitrate_kbps"),
        sample_rate=info.get("sample_rate"),
        channels=info.get("channels"),
        duration=info.get("duration"),
    )
    report.lossless_container = report.codec in _LOSSLESS_CODECS

    # Si decodifica al campionamento del file: ricampionare a 48 kHz un file
    # a 96 kHz sposterebbe il Nyquist e falserebbe la misura.
    probe_rate = max(44100, min(96000, report.sample_rate or 48000))
    report.cutoff_hz = measure_cutoff(path, report.duration, probe_rate)
    report.tier = _tier_for(report.cutoff_hz)

    # Un contenitore lossless con un muro spettrale netto è un lossy travestito.
    if report.lossless_container and report.cutoff_hz and report.cutoff_hz < 20500:
        report.transcoded = True
        report.notes.append(
            f"contenitore lossless ma spettro tagliato a {report.cutoff_hz / 1000:.1f} kHz: "
            "la sorgente era compressa")

    # Un bitrate alto dichiarato con uno spettro basso è il caso classico del
    # ri-caricamento: MP3 128 ricodificato a 320.
    if (not report.lossless_container and (report.bitrate_kbps or 0) >= 256
            and report.cutoff_hz and report.cutoff_hz < 19000):
        report.transcoded = True
        report.notes.append(
            f"dichiarati {report.bitrate_kbps} kbps ma spettro fino a "
            f"{report.cutoff_hz / 1000:.1f} kHz: ricodifica da sorgente peggiore")

    if report.sample_rate and report.sample_rate < 44100:
        report.notes.append(f"campionamento {report.sample_rate} Hz sotto lo standard CD")
    if report.channels == 1:
        report.notes.append("audio mono")

    report.score = _score(report)
    return report


def _score(report: QualityReport) -> float:
    """Punteggio 0-100 per confrontare candidati fra loro.

    Il taglio spettrale pesa più del bitrate dichiarato, perché è l'unico dato
    che non si può falsificare ri-codificando.
    """
    if report.cutoff_hz is None:
        # Senza misura ci si fida solo del dichiarato, con prudenza.
        base = min(60.0, (report.bitrate_kbps or 96) / 320 * 60)
        return round(base, 1)

    # 15 kHz → 0, 21 kHz → 70: sotto i 15 kHz il file non è utilizzabile in
    # un impianto, sopra i 21 non c'è più taglio da penalizzare.
    spectral = max(0.0, min(70.0, (report.cutoff_hz - 15000) / 6000 * 70))

    container = 0.0
    if report.lossless_container and not report.transcoded:
        container = 20.0
    elif report.bitrate_kbps:
        container = min(15.0, report.bitrate_kbps / 320 * 15)

    penalty = 0.0
    if report.transcoded:
        penalty += 15.0
    if report.channels == 1:
        penalty += 10.0
    if report.sample_rate and report.sample_rate < 44100:
        penalty += 10.0

    return round(max(0.0, min(100.0, spectral + container + 10.0 - penalty)), 1)

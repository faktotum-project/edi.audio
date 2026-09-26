#!/usr/bin/env python3
"""Analisi audio: BPM, griglia, chiave, struttura.

**Questo file gira dentro il venv 3.12**, non nell'interprete dell'app: librosa
dipende da numba, che non ha ancora ruote per Python 3.14. Il contratto con il
resto dell'applicazione è volutamente minimo — un percorso in ingresso, un JSON
su stdout — così l'app principale non deve nemmeno sapere che librosa esiste.

    python analyzer_worker.py traccia.flac
"""
import argparse
import json
import sys

import numpy as np

# Profili di Krumhansl-Schmuckler: quanto "pesa" ciascun grado della scala in
# un brano realmente in quella tonalità. Correlando il cromagramma medio con
# le 24 rotazioni di questi profili si ottiene la chiave.
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                           2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                           2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

_PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Scostamento massimo dei beat da una griglia rigida (in frazione del periodo)
# perché il brano si consideri a tempo fisso: sotto questa soglia si emette una
# sola marca di griglia invece di tante.
_TEMPO_STABLE_THRESHOLD = 0.05

# Risoluzione dell'analisi degli onset, in campioni.
_HOP_LENGTH = 256

# Intervallo di tempi plausibile per un brano ballabile. Serve a risolvere
# l'ambiguità di ottava: un rilevatore che restituisce 65 BPM su un pezzo a 130
# non ha sbagliato la griglia, ha contato un beat su due.
_MIN_BPM = 76.0
_MAX_BPM = 185.0

# Quanta energia devono avere le posizioni intermedie, rispetto a quelle sulla
# griglia, perché si concluda che il beat vero è il doppio.
_HALF_BEAT_ENERGY_RATIO = 0.45


def detect_key(y, sr):
    """Tonica, modo e confidenza dal cromagramma CQT.

    Si usa il CQT e non l'STFT perché è allineato ai semitoni, e la mediana
    invece della media perché un finale tenuto o un intro lungo non devono
    dominare la stima.
    """
    import librosa

    harmonic = librosa.effects.harmonic(y, margin=4)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=sr)
    profile = np.median(chroma, axis=1)
    if profile.sum() <= 0:
        return None
    profile = profile / profile.sum()

    scores = []
    for shift in range(12):
        rotated = np.roll(profile, -shift)
        scores.append((np.corrcoef(rotated, _MAJOR_PROFILE)[0, 1], shift, False))
        scores.append((np.corrcoef(rotated, _MINOR_PROFILE)[0, 1], shift, True))
    scores = [s for s in scores if not np.isnan(s[0])]
    if not scores:
        return None
    scores.sort(reverse=True)

    best_score, shift, is_minor = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0.0
    return {
        "tonic": _PITCHES[shift],
        "is_minor": bool(is_minor),
        # Il distacco dal secondo classificato dice molto più del punteggio
        # assoluto: due tonalità vicine si somigliano sempre parecchio.
        "confidence": float(max(0.0, best_score - runner_up)),
    }


def _phase_concentration(weights, times, period):
    """Quanto l'energia degli onset si concentra su una griglia di dato periodo.

    Si sommano i frame dell'inviluppo come vettori sul cerchio unitario, con
    l'angolo dato dalla posizione del frame dentro il periodo. Se gli onset
    cadono tutti in fase il risultato tende a 1; se sono sparsi si annullano
    fra loro e tende a 0. È una misura globale, che usa tutto il brano invece
    di fidarsi di dove il rilevatore ha deciso che stanno i beat.
    """
    return (weights * np.exp(2j * np.pi * times / period)).sum() / weights.sum()


def low_band_onsets(y, sr, hop_length):
    """Inviluppo di onset delle sole basse frequenze.

    Serve a decidere la *fase* della griglia. Sull'intero spettro il charleston
    in levare produce più flusso spettrale della cassa — è un colpo di rumore a
    banda larga contro una sinusoide grave — e il baricentro delle fasi finisce
    per agganciarsi al levare, sfasando la griglia di mezzo beat. Sotto i
    200 Hz resta essenzialmente la cassa, che è ciò che in un brano da club
    definisce dov'è l'uno.
    """
    import librosa

    # Poche bande: sotto i 200 Hz non c'è spazio per trentadue filtri mel, e
    # chiederli produce filtri vuoti (e un avviso di librosa) senza aggiungere
    # informazione.
    spec = librosa.feature.melspectrogram(
        y=y, sr=sr, hop_length=hop_length, n_mels=8, fmin=20, fmax=200)
    return librosa.onset.onset_strength(S=librosa.power_to_db(spec), sr=sr,
                                        hop_length=hop_length, aggregate=np.median)


def estimate_grid(onset_env, sr, hop_length, seed_period, phase_env=None):
    """Periodo e fase della griglia, stimati sull'inviluppo di onset.

    Perché non usare direttamente i beat di `beat_track`: quei tempi sono
    quantizzati alla risoluzione dell'analisi e contengono beat inseriti o
    mancati, e ogni statistica costruita sopra (mediana, media, regressione)
    eredita quegli errori. Su un brano di prova a 128 BPM esatti le stime
    ricavate dai beat sbagliavano fra 0.2 e 1.2 BPM — sufficiente a far
    scollare la griglia di oltre un decimo di secondo a fine traccia. La stessa
    misura fatta sull'inviluppo restituisce 127.9998.

    L'errore conta perché la griglia di Serato è un modello a tempo costante:
    un BPM sbagliato non sposta un beat, li sposta tutti, sempre di più via via
    che il brano avanza.
    """
    times = librosa_frame_times(onset_env, sr, hop_length)
    weights = onset_env - onset_env.mean()
    weights[weights < 0] = 0
    if weights.sum() <= 0:
        return None

    # Si esplora ±18% attorno alla stima iniziale: abbastanza per correggerla,
    # troppo poco per scivolare su metà o doppio del tempo.
    grid = np.linspace(seed_period * 0.85, seed_period * 1.18, 20001)
    scores = np.abs(np.array([_phase_concentration(weights, times, p) for p in grid]))

    peak = int(scores.argmax())
    period = float(grid[peak])
    # Interpolazione parabolica fra i tre punti attorno al massimo, per non
    # essere limitati dal passo della griglia di ricerca
    if 0 < peak < len(grid) - 1:
        y0, y1, y2 = scores[peak - 1], scores[peak], scores[peak + 1]
        denominator = y0 - 2 * y1 + y2
        if denominator != 0:
            period += 0.5 * (y0 - y2) / denominator * (grid[1] - grid[0])

    # Il periodo si misura su tutto lo spettro, la fase sulla sola banda bassa
    strength = np.abs(_phase_concentration(weights, times, period))
    phase_weights = weights
    if phase_env is not None and len(phase_env) == len(onset_env):
        phase_weights = phase_env - phase_env.mean()
        phase_weights[phase_weights < 0] = 0
        if phase_weights.sum() <= 0:
            phase_weights = weights

    vector = _phase_concentration(phase_weights, times, period)
    # L'angolo del vettore risultante dà l'istante del primo beat della griglia
    offset = float((np.angle(vector) / (2 * np.pi)) * period % period)

    return {
        "period": period,
        "offset": offset,
        "confidence": float(strength),
        "weights": weights,
        "frame_times": times,
    }


def librosa_frame_times(onset_env, sr, hop_length):
    import librosa
    return librosa.frames_to_time(np.arange(len(onset_env)), sr=sr, hop_length=hop_length)


def _energy_at(weights, frame_times, positions):
    """Energia media dell'inviluppo agli istanti indicati."""
    if len(positions) == 0:
        return 0.0
    step = frame_times[1] - frame_times[0]
    index = np.clip(np.round(positions / step).astype(int), 0, len(weights) - 1)
    return float(np.mean(weights[index]))


def correct_octave(grid_info, period, duration):
    """Risolve l'ambiguità fra un tempo e la sua metà o il suo doppio.

    La concentrazione di fase non può distinguerli: se i beat cadono su una
    griglia di periodo `p`, cadono anche su quella di periodo `p/2`, e il
    punteggio è identico. Serve una domanda diversa — *le posizioni intermedie
    hanno anch'esse un colpo?* Se fra un beat e l'altro c'è altrettanta
    energia, il beat vero è quello a metà e il tempo va raddoppiato.

    Alla fine si riporta comunque il risultato nell'intervallo ballabile: un
    brano a 65 BPM quasi sempre è un 130 contato male.
    """
    weights = grid_info["weights"]
    times = grid_info["frame_times"]
    offset = grid_info["offset"]

    for _ in range(2):
        if 60.0 / (period / 2) > _MAX_BPM:
            break
        on_grid = np.arange(offset, duration, period)
        between = np.arange(offset + period / 2, duration, period)
        energy_on = _energy_at(weights, times, on_grid)
        energy_between = _energy_at(weights, times, between)
        if energy_on <= 0 or energy_between / energy_on < _HALF_BEAT_ENERGY_RATIO:
            break
        period /= 2

    # Rete di sicurezza: fuori dall'intervallo ballabile si raddoppia o dimezza
    while 60.0 / period < _MIN_BPM and 60.0 / (period / 2) <= _MAX_BPM:
        period /= 2
    while 60.0 / period > _MAX_BPM:
        period *= 2

    return period


def grid_stability(grid_info, period):
    """Scostamento del tempo fra prima e seconda metà del brano.

    Un brano suonato a mano o con un cambio di tempo dà due stime diverse: in
    quel caso la griglia a tempo fisso non basta e servono più marche.
    """
    weights = grid_info["weights"]
    times = grid_info["frame_times"]
    middle = len(weights) // 2

    periods = []
    for lo, hi in ((0, middle), (middle, len(weights))):
        w, t = weights[lo:hi], times[lo:hi]
        if w.sum() <= 0:
            continue
        search = np.linspace(period * 0.96, period * 1.04, 2001)
        scores = np.abs(np.array([_phase_concentration(w, t, p) for p in search]))
        periods.append(float(search[scores.argmax()]))

    if len(periods) < 2:
        return 0.0
    return abs(periods[0] - periods[1]) / period


def detect_beats(y, sr):
    """Beat, BPM e stabilità del tempo.

    `beat_track` serve solo a dare una stima di partenza e i tempi dei singoli
    beat (che alimentano la segmentazione); il tempo vero viene poi misurato
    sull'inviluppo, che è molto più preciso.
    """
    import librosa

    # hop più corto del default (512): i tempi dei beat vengono quantizzati a
    # questa risoluzione, e 11 ms di errore su un beat da 470 ms si vedono
    # eccome quando la griglia deve reggere per sei minuti.
    onset_env = librosa.onset.onset_strength(
        y=y, sr=sr, hop_length=_HOP_LENGTH, aggregate=np.median)
    tempo, beat_frames = librosa.beat.beat_track(
        onset_envelope=onset_env, sr=sr, hop_length=_HOP_LENGTH,
        trim=False, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr, hop_length=_HOP_LENGTH)

    if len(beat_times) < 4:
        return None

    intervals = np.diff(beat_times)
    seed = float(np.median(intervals))
    if seed <= 0:
        return None

    grid = estimate_grid(onset_env, sr, _HOP_LENGTH, seed,
                         phase_env=low_band_onsets(y, sr, _HOP_LENGTH))
    if grid is None:
        return None

    duration = len(y) / sr
    period = correct_octave(grid, grid["period"], duration)
    spread = grid_stability(grid, period)

    # Griglia analitica a partire da periodo e fase: è il modello che Serato e
    # Mixxx useranno comunque, quindi tanto vale costruirlo direttamente invece
    # di ricavarlo da beat rilevati uno a uno.
    count = int((duration - grid["offset"]) / period) + 1
    grid_beats = grid["offset"] + np.arange(max(count, 0)) * period

    return {
        "bpm": 60.0 / period,
        "beat_times": [float(t) for t in grid_beats],
        "detected_beats": [float(t) for t in beat_times],
        "onset_env": onset_env,
        "tempo_spread": float(spread),
        "is_stable": bool(spread < _TEMPO_STABLE_THRESHOLD),
        "grid_confidence": float(grid["confidence"]),
    }


def detect_downbeat_phase(beat_times, onset_env, sr, hop_length=_HOP_LENGTH):
    """Quale dei quattro beat è l'uno.

    librosa non individua i downbeat. In 4/4 il primo movimento è quasi sempre
    quello più accentato, quindi si prova ognuna delle quattro fasi possibili e
    si tiene quella la cui somma di onset strength è massima.
    """
    import librosa

    frames = librosa.time_to_frames(beat_times, sr=sr, hop_length=hop_length)
    frames = np.clip(frames, 0, len(onset_env) - 1)
    strengths = onset_env[frames]

    best_phase, best_score = 0, -np.inf
    for phase in range(4):
        score = float(np.sum(strengths[phase::4]))
        if score > best_score:
            best_phase, best_score = phase, score
    return best_phase


def detect_structure(y, sr, beat_times):
    """Confini di sezione ed energia, la materia prima dei cue.

    La segmentazione agglomerativa sui cromagrammi allineati ai beat trova i
    punti in cui cambia l'armonia; l'RMS per sezione dice quali di quelle
    sezioni sono il drop e quali il breakdown.
    """
    import librosa

    if len(beat_times) < 8:
        return {"boundaries": [], "sections": []}

    beat_frames = librosa.time_to_frames(beat_times, sr=sr)
    beat_frames = np.clip(beat_frames, 0, None)

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_sync = librosa.util.sync(chroma, beat_frames, aggregate=np.median)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_sync = librosa.util.sync(mfcc, beat_frames, aggregate=np.median)
    features = np.vstack([librosa.util.normalize(chroma_sync, axis=0),
                          librosa.util.normalize(mfcc_sync, axis=0)])

    # ~una sezione ogni 16 battute, con un minimo e un massimo ragionevoli
    k = int(np.clip(len(beat_times) / 64, 4, 12))
    try:
        bound_beats = librosa.segment.agglomerative(features, k)
    except Exception:
        return {"boundaries": [], "sections": []}

    bound_times = [float(beat_times[min(b, len(beat_times) - 1)]) for b in bound_beats]
    bound_times = sorted(set([0.0] + bound_times + [float(len(y) / sr)]))

    # Sezioni più corte di 8 battute non sono sezioni: sono transizioni che la
    # segmentazione ha scambiato per confini. Tenerle riempirebbe la struttura
    # di frammenti e manderebbe fuori strada la ricerca del drop.
    if len(beat_times) > 1:
        min_length = (beat_times[-1] - beat_times[0]) / (len(beat_times) - 1) * 32
        merged = [bound_times[0]]
        for boundary in bound_times[1:-1]:
            if boundary - merged[-1] >= min_length:
                merged.append(boundary)
        merged.append(bound_times[-1])
        bound_times = merged

    rms = librosa.feature.rms(y=y)[0]
    rms_times = librosa.times_like(rms, sr=sr)
    peak = float(np.max(rms)) or 1.0

    sections = []
    for start, end in zip(bound_times, bound_times[1:]):
        mask = (rms_times >= start) & (rms_times < end)
        if not mask.any():
            continue
        sections.append({
            "start": start,
            "end": end,
            # Energia normalizzata sul picco del brano: confrontabile fra
            # tracce masterizzate a volumi diversi.
            "energy": float(np.mean(rms[mask]) / peak),
            "peak": float(np.max(rms[mask]) / peak),
        })

    return {"boundaries": bound_times, "sections": sections}


def first_sound(y, sr, threshold_ratio=0.10):
    """Istante in cui il brano comincia davvero a suonare.

    Serve perché molti file hanno silenzio o un fade quasi impercettibile in
    testa, e un cue di mix-in piazzato al campione zero è inutilizzabile.
    """
    import librosa

    rms = librosa.feature.rms(y=y)[0]
    times = librosa.times_like(rms, sr=sr)
    above = np.where(rms > np.max(rms) * threshold_ratio)[0]
    return float(times[above[0]]) if len(above) else 0.0


def analyze(path):
    import librosa

    y, sr = librosa.load(path, sr=None, mono=True)
    duration = float(len(y) / sr)

    beats = detect_beats(y, sr)
    if beats is None:
        return {"ok": False, "error": "impossibile individuare i beat", "duration": duration}

    phase = detect_downbeat_phase(beats["beat_times"], beats["onset_env"], sr)
    beat_times = beats["beat_times"]
    downbeats = beat_times[phase::4]

    return {
        "ok": True,
        "duration": duration,
        "sample_rate": int(sr),
        "bpm": round(beats["bpm"], 3),
        "tempo_spread": round(beats["tempo_spread"], 5),
        "tempo_stable": beats["is_stable"],
        # Quanto l'energia del brano si allinea davvero alla griglia trovata.
        # Sotto ~0.15 la griglia è un'ipotesi debole e va guardata a occhio.
        "grid_confidence": round(beats["grid_confidence"], 4),
        "beat_times": [round(t, 5) for t in beat_times],
        "downbeat_times": [round(t, 5) for t in downbeats],
        "first_beat": round(downbeats[0], 5) if len(downbeats) else 0.0,
        "first_sound": round(first_sound(y, sr), 5),
        "key": detect_key(y, sr),
        "structure": detect_structure(y, sr, beat_times),
    }


def _jsonable(value):
    """Converte i tipi numpy che finiscono nel risultato."""
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"non serializzabile: {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description="Analisi DJ di un file audio (JSON su stdout).")
    parser.add_argument("path")
    args = parser.parse_args()

    try:
        result = analyze(args.path)
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    json.dump(result, sys.stdout, default=_jsonable)
    sys.stdout.write("\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

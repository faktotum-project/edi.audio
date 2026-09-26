"""Orchestrazione: da un URL (o da un file già scaricato) a traccia mixabile.

Fasi, nell'ordine, ognuna delle quali segnala il proprio avanzamento:

    sorgente → scarico → analizzo → cue → tag → rinomino → pronto

Il principio che governa la gestione degli errori: una fase che fallisce non
deve buttare via il lavoro delle precedenti. Se l'analisi non parte perché
manca il venv, il file scaricato resta comunque sul disco con i suoi metadata;
sarà `rescan` a completarlo più tardi.
"""
import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import analysis, cues, export, naming, quality, sources, tags


@dataclass
class Result:
    """Esito della preparazione di una traccia."""
    ok: bool
    path: str | None = None
    artist: str = ""
    title: str = ""
    bpm: float | None = None
    key: str = ""
    camelot: str = ""
    quality_label: str = ""
    quality_score: float = 0.0
    source: str = ""
    cues: int = 0
    loops: int = 0
    preserved: bool = False
    notes: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"errore: {self.error}"
        bits = [f"{round(self.bpm)} BPM" if self.bpm else "",
                f"{self.key} ({self.camelot})" if self.key and self.camelot else self.key or self.camelot]
        if self.preserved:
            bits.append("cue conservati")
        else:
            bits += [f"{self.cues} cue", f"{self.loops} loop"]
        bits.append(self.quality_label)
        return " · ".join(b for b in bits if b)


def _emit(progress, phase: str, message: str = ""):
    if progress:
        progress(phase, message)


def _split_title(raw: str) -> tuple[str, str]:
    """Separa "Artista - Titolo" quando l'estrattore non dà i campi separati."""
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return left.strip(), right.strip()
    return "", raw.strip()


def _strip_artist_prefix(title: str, artist: str) -> str:
    """Toglie dal titolo l'artista che alcune sorgenti ci ripetono dentro.

    Senza questo si ottengono nomi come «Daydream Affiliate - Daydream
    Affiliate - Playback»: il campo `track` di certi caricamenti contiene già
    «Artista - Titolo», e noi lo rimettiamo davanti all'artista.
    """
    if not title or not artist:
        return title
    lowered = title.lower()
    prefix = artist.lower()
    for sep in (" - ", " – ", " — "):
        if lowered.startswith(prefix + sep):
            return title[len(artist) + len(sep):].strip() or title
    return title


def _to_flac(src: Path, dest_dir: Path) -> Path:
    """Converte in FLAC preservando il decode della sorgente.

    Non si guadagna qualità da una sorgente lossy, ma non se ne perde altra: è
    il contenitore, non una promessa sul contenuto. La differenza rispetto a
    ri-comprimere in MP3 è che lì la perdita si somma.
    """
    dest = dest_dir / (src.stem + ".flac")
    if src.suffix.lower() == ".flac":
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-c:a", "flac", "-compression_level", "8", str(dest)],
        check=True, capture_output=True,
    )
    return dest


def download_best(query_or_url: str, output_dir, *, progress=None,
                  search_alternatives: bool = True,
                  allow_variants: bool = False) -> tuple[Path, sources.Candidate, dict]:
    """Scarica la migliore sorgente disponibile e restituisce il FLAC grezzo.

    Se `query_or_url` è un URL, quello diventa un candidato fra gli altri: se
    la ricerca per titolo trova qualcosa di misurabilmente migliore, si scarica
    quello. È il comportamento richiesto — il link è un suggerimento, non un
    vincolo — e la sorgente scelta viene sempre dichiarata.
    """
    import yt_dlp

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    is_url = query_or_url.startswith(("http://", "https://"))

    _emit(progress, "sorgente", "identifico il brano")
    candidates: list[sources.Candidate] = []
    given = None

    if is_url:
        given = sources.candidate_for_url(query_or_url)
        if given:
            candidates.append(given)
        artist, title = _split_title(given.title if given else "")
    else:
        artist, title = _split_title(query_or_url)
        if not title:
            title = query_or_url

    canonical = sources.canonical_track(title or query_or_url, artist)
    if canonical:
        artist = canonical["artist"] or artist
        title = canonical["title"] or title

    if search_alternatives and title:
        _emit(progress, "sorgente", f"cerco alternative per «{artist} - {title}»")
        candidates += sources.search_candidates(artist, title)

    if not candidates:
        raise RuntimeError("nessuna sorgente trovata per questo brano")

    _emit(progress, "sorgente", f"valuto {len(candidates)} candidati")
    best = sources.choose(
        candidates,
        target_duration=(canonical or {}).get("duration"),
        artist=artist, title=title, allow_variants=allow_variants,
    )
    if best is None:
        raise RuntimeError("nessun candidato utilizzabile")

    if given is not None and best is not given:
        _emit(progress, "sorgente",
              f"il link fornito è stato scartato: {best.describe()} è migliore "
              f"({best.score:.0f} contro {given.score:.0f})")

    _emit(progress, "scarico", best.describe())
    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(Path(tmp) / "%(title)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "proxy": "",
            "writethumbnail": True,
        }
        if best.tier == 1:
            opts["cookiesfrombrowser"] = (sources.COOKIE_BROWSER,)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(best.url, download=True)

        audio_files = [p for p in Path(tmp).iterdir()
                       if p.suffix.lower() not in (".jpg", ".png", ".webp")]
        if not audio_files:
            raise RuntimeError("il download non ha prodotto alcun file audio")

        cover = None
        images = [p for p in Path(tmp).iterdir() if p.suffix.lower() in (".jpg", ".png")]
        if images:
            cover = images[0].read_bytes()

        flac_path = _to_flac(max(audio_files, key=lambda p: p.stat().st_size), output_dir)

    final_artist = (info or {}).get("artist") or artist
    final_title = (info or {}).get("track") or title or (info or {}).get("title", "")

    meta = {
        "artist": final_artist,
        "title": _strip_artist_prefix(final_title, final_artist),
        "album": (info or {}).get("album"),
        "date": str((info or {}).get("release_year") or "") or None,
        "genre": (info or {}).get("genre") or (canonical or {}).get("genre") or None,
        "cover": cover,
    }
    return flac_path, best, meta


def prepare_file(path, *, progress=None, metadata: dict | None = None,
                 candidate: sources.Candidate | None = None,
                 rename: bool = True, force: bool = False) -> Result:
    """Analizza, cue-a e tagga un FLAC già presente sul disco.

    Se griglia o cue risultano corretti a mano — tipicamente in Mixxx, che
    riscrive i tag nel file quando è configurato per farlo — quelli non si
    toccano: si aggiornano solo metadata, guadagno e nome. Il lavoro fatto a
    mano vale più di qualunque automatismo, e va protetto anche dal nostro.
    `force=True` scavalca la protezione e riscrive tutto.
    """
    path = Path(path)
    metadata = dict(metadata or {})
    cover = metadata.pop("cover", None)

    if not metadata.get("artist") and not metadata.get("title"):
        artist, title = _split_title(path.stem)
        metadata.setdefault("artist", artist)
        metadata.setdefault("title", title or path.stem)

    edits = None
    if not force:
        try:
            edits = tags.manual_edits(path)
        except Exception:
            edits = None
    if edits:
        _emit(progress, "analizzo",
              "correzioni manuali rilevate: griglia e cue non verranno toccati")

    _emit(progress, "analizzo", "beat, chiave e struttura")
    try:
        result = analysis.analyze(path)
    except analysis.AnalysisUnavailable as exc:
        return Result(ok=False, path=str(path), error=str(exc),
                      artist=metadata.get("artist", ""), title=metadata.get("title", ""))
    except Exception as exc:
        return Result(ok=False, path=str(path), error=f"analisi fallita: {exc}",
                      artist=metadata.get("artist", ""), title=metadata.get("title", ""))

    _emit(progress, "cue", "individuo drop, break e punti di mix")
    layout = cues.build(result)

    _emit(progress, "tag", "scrivo beatgrid e cue nel file")
    report = candidate.report if candidate and candidate.report else quality.assess(path)

    # Descrizione della sorgente: quella del candidato è la più accurata perché
    # fatta quando il codec di origine era ancora noto. Se stiamo ripreparando
    # un file già passato di qui, si riprende quella scritta allora invece di
    # ricavarne una peggiore dal FLAC.
    if candidate is not None:
        source = candidate.describe()
    else:
        try:
            source = tags.read(path).get("source") or ""
        except Exception:
            source = ""

    gain = analysis.replay_gain(path)
    summary = tags.write(path, analysis=result, layout=layout, quality_report=report,
                         metadata=metadata, cover=cover, replay_gain=gain, source=source,
                         preserve_cues=bool(edits))

    final_path = path
    if rename:
        _emit(progress, "rinomino", "")
        target = path.parent / naming.track_filename(
            summary.get("bpm"), summary.get("camelot", ""),
            metadata.get("artist", ""), metadata.get("title", ""),
            key=summary.get("key", ""))
        if target.resolve() != path.resolve():
            target = naming.deduplicate(target)
            path.rename(target)
            final_path = target

    notes = list(layout.notes)
    if edits:
        notes = [f"correzioni manuali conservate ({', '.join(edits['changed'])})"]

    return Result(
        ok=True,
        path=str(final_path),
        artist=metadata.get("artist", ""),
        title=metadata.get("title", ""),
        bpm=summary.get("bpm"),
        key=summary.get("key", ""),
        camelot=summary.get("camelot", ""),
        quality_label=report.label() if report else "",
        quality_score=report.score if report else 0.0,
        source=candidate.source if candidate else "",
        cues=summary.get("cues", 0),
        loops=summary.get("loops", 0),
        preserved=bool(edits),
        notes=notes,
    )


def prepare(query_or_url: str, output_dir, *, progress=None,
            search_alternatives: bool = True, allow_variants: bool = False) -> Result:
    """Pipeline completa: scarica la sorgente migliore e la prepara."""
    try:
        path, candidate, metadata = download_best(
            query_or_url, output_dir, progress=progress,
            search_alternatives=search_alternatives, allow_variants=allow_variants)
    except Exception as exc:
        return Result(ok=False, error=str(exc))

    result = prepare_file(path, progress=progress, metadata=metadata, candidate=candidate)
    result.source = candidate.describe()
    if not result.quality_label and candidate.report:
        result.quality_label = candidate.report.label()
    _emit(progress, "pronto", result.summary())
    return result


def default_workers() -> int:
    """Quante tracce analizzare insieme.

    L'analisi è a calcolo puro e occupa un core alla volta, quindi conviene
    tenerne occupati quasi tutti lasciandone un paio liberi per l'interfaccia e
    per i download che intanto proseguono.
    """
    override = os.environ.get("DJ_WORKERS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    return max(1, min(8, (os.cpu_count() or 2) - 2))


def _pending(directory, force: bool = False) -> list[Path]:
    """FLAC che hanno ancora bisogno di essere preparati.

    Con `force` si prendono tutti, comprese le tracce già pronte: serve quando
    è cambiato il modo in cui costruiamo cue e griglia e vogliamo riapplicarlo
    a una libreria esistente.
    """
    pending = []
    for path in sorted(Path(directory).rglob("*.flac")):
        if force:
            pending.append(path)
            continue
        try:
            existing = tags.read(path)
        except Exception:
            existing = {}
        # Una traccia già preparata e mai ritoccata non ha nulla da guadagnare
        # da un altro giro; una ritoccata a mano sì, perché metadata e nome
        # vanno aggiornati anche se griglia e cue restano quelli.
        if existing.get("bpm_locked") and existing.get("cues") and not existing.get("edited_manually"):
            continue
        pending.append(path)
    return pending


def _run_parallel(items, work, *, workers: int, progress=None, label=None):
    """Esegue `work` su ogni elemento in parallelo, con avanzamento ordinato.

    Il callback di avanzamento viene serializzato con un lock: senza, le righe
    di più tracce si intrecciano e il log diventa illeggibile.
    """
    lock = threading.Lock()
    total = len(items)
    done = 0
    results = [None] * total

    def run(index_item):
        nonlocal done
        index, item = index_item
        results[index] = work(item)
        with lock:
            done += 1
            if progress:
                name = label(item) if label else str(item)
                progress("traccia", f"[{done}/{total}] {name}")

    if workers <= 1:
        for pair in enumerate(items):
            run(pair)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, enumerate(items)))
    return results


def rescan(directory, *, progress=None, rename: bool = True,
           workers: int | None = None, force: bool = False) -> list[Result]:
    """Prepara tutti i FLAC di una cartella che non sono ancora pronti.

    Serve per la libreria che esiste già: si passa una cartella di file
    scaricati in passato e ne escono tracce con griglia e cue. Le tracce si
    analizzano in parallelo, perché in seriale una libreria di un centinaio di
    brani richiederebbe più di un'ora.
    """
    pending = _pending(directory, force=force)
    if not pending:
        return []

    workers = workers if workers is not None else default_workers()
    _emit(progress, "analizzo", f"{len(pending)} tracce, {workers} in parallelo")

    return _run_parallel(
        pending,
        lambda path: prepare_file(path, rename=rename, force=force),
        workers=workers, progress=progress, label=lambda p: p.name,
    )


def expand_queries(queries: list[str], *, progress=None, limit: int = 500) -> list[str]:
    """Sostituisce ogni playlist con le tracce che contiene.

    Le voci che non sono playlist restano come sono, così l'elenco può
    mescolare link singoli, playlist e titoli scritti a mano.
    """
    expanded = []
    for query in queries:
        if not sources.is_playlist(query):
            expanded.append(query)
            continue
        _emit(progress, "coda", f"espando la playlist {query[:60]}")
        tracks = sources.expand_playlist(query, limit=limit)
        if tracks:
            _emit(progress, "coda", f"{len(tracks)} tracce nella playlist")
            expanded += tracks
        else:
            # Meglio provarci come traccia singola che scartarla in silenzio
            _emit(progress, "coda", "playlist vuota o illeggibile: la tratto come traccia singola")
            expanded.append(query)
    return expanded


def prepare_many(queries: list[str], output_dir, *, progress=None,
                 workers: int | None = None, search_alternatives: bool = True,
                 allow_variants: bool = False) -> list[Result]:
    """Prepara più tracce insieme, da URL o da titoli."""
    if not queries:
        return []

    workers = workers if workers is not None else default_workers()
    _emit(progress, "coda", f"{len(queries)} tracce, {workers} in parallelo")

    return _run_parallel(
        queries,
        lambda query: prepare(query, output_dir,
                              search_alternatives=search_alternatives,
                              allow_variants=allow_variants),
        workers=workers, progress=progress, label=lambda q: q[:70],
    )

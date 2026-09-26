"""Scelta della sorgente audio migliore per un brano.

Il principio: un link non è una promessa di qualità. Quando l'utente incolla un
URL, quello diventa *un candidato fra gli altri* — se cercando per titolo si
trova qualcosa di misurabilmente migliore, si scarica quello e lo si dichiara.

Ordine di fiducia delle sorgenti:

  1. store dove l'utente ha acquistato (Bandcamp, Beatport, Juno) — lossless vero
  2. Bandcamp pubblico — spesso FLAC scaricabile
  3. YouTube / YouTube Music — tetto pratico ~160 kbps Opus
  4. SoundCloud — utile per edit e bootleg che non esistono altrove

Gli adapter degli store dipendono dall'HTML dei siti e sono la parte che si
romperà per prima: sono isolati qui e falliscono in modo pulito, ripiegando
sulle fonti libere.
"""
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import quality

# Parole che segnalano una versione diversa da quella cercata. Non sono un
# divieto: se l'utente chiede esplicitamente un remix, la penalità sparisce.
_VARIANT_MARKERS = [
    "live", "cover", "karaoke", "instrumental", "acapella", "a cappella",
    "sped up", "speed up", "slowed", "nightcore", "8d audio", "reverb",
    "lyrics", "lyric video", "tribute", "remake", "midi",
]

# Browser da cui prendere i cookie per gli store dove l'utente è autenticato.
COOKIE_BROWSER = os.environ.get("DJ_COOKIE_BROWSER", "firefox")


@dataclass
class Candidate:
    """Una possibile sorgente per il brano."""
    url: str
    title: str = ""
    uploader: str = ""
    duration: float | None = None
    source: str = "web"
    tier: int = 5              # più basso = più affidabile
    abr: float | None = None   # bitrate dichiarato dall'estrattore
    codec: str = ""
    report: quality.QualityReport | None = None
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [self.source]
        if self.report:
            parts.append(self.report.label())
        elif self.abr:
            parts.append(f"{self.abr:.0f} kbps {self.codec}")
        return " · ".join(parts)


# ---------------------------------------------------------------------------
# Identificazione del brano
# ---------------------------------------------------------------------------

# Sotto questa somiglianza il risultato di MusicBrainz si scarta: una durata
# canonica sbagliata è peggio di nessuna durata, perché farebbe respingere
# proprio i candidati giusti.
_MIN_CANONICAL_MATCH = 60


def _similarity(a: str, b: str) -> float:
    """Somiglianza fra due stringhe, 0-100.

    Si usa `token_sort_ratio` e non `token_set_ratio`: il secondo dà 100 a
    «Switch Disco - Bicep x Nelly Furtado - Glue x Say It Right» per la
    richiesta «Bicep Glue», perché tutte le parole cercate ci sono dentro.
    Il primo tiene conto anche di quanto testo c'è in più, e scende a 30.
    """
    try:
        from rapidfuzz import fuzz
        return float(fuzz.token_sort_ratio(a, b))
    except ImportError:
        words_a, words_b = set(a.lower().split()), set(b.lower().split())
        return 100.0 * len(words_a & words_b) / max(len(words_a | words_b), 1)


def canonical_track(title: str, artist: str = "") -> dict | None:
    """Artista, titolo e durata canonici secondo MusicBrainz.

    La durata canonica è il filtro anti-spazzatura più efficace che abbiamo:
    scarta da sola intro edit, versioni tagliate e nightcore, che sbagliano la
    durata anche quando il titolo è identico.

    Quando artista e titolo sono distinti si interroga per campi. La ricerca a
    testo libero è inaffidabile: «Bicep Glue» non restituisce affatto il brano
    dei Bicep, ma dieci brani intitolati «Bicep» di artisti diversi. Se il
    risultato migliore non somiglia abbastanza alla richiesta si preferisce non
    restituire nulla.
    """
    try:
        import musicbrainzngs
    except ImportError:
        return None

    musicbrainzngs.set_useragent("edi.audio", "1.0", "https://edi.audio")

    def escape(text: str) -> str:
        return text.replace('"', " ").strip()

    if artist:
        query = f'artist:"{escape(artist)}" AND recording:"{escape(title)}"'
    else:
        query = f'recording:"{escape(title)}"'
    wanted = f"{artist} {title}".strip()

    try:
        result = musicbrainzngs.search_recordings(query=query, limit=10)
    except Exception:
        return None

    def genre_of(rec) -> str:
        credits = rec.get("artist-credit") or []
        if not credits or "artist" not in credits[0]:
            return ""
        return artist_genre(credits[0]["artist"].get("id", ""))

    best, best_score = None, -1.0
    durations: list[float] = []

    for rec in result.get("recording-list", []):
        credits = rec.get("artist-credit") or []
        found_artist = credits[0]["artist"]["name"] if credits and "artist" in credits[0] else ""
        found_title = rec.get("title", "")
        if not found_artist or not found_title:
            continue

        score = _similarity(wanted, f"{found_artist} {found_title}")
        if score < _MIN_CANONICAL_MATCH:
            continue
        # Il punteggio di MusicBrainz vale solo a parità di somiglianza
        score += float(rec.get("ext:score", 0)) / 100

        length = rec.get("length")
        if length:
            durations.append(float(length) / 1000)

        if score > best_score:
            best_score = score
            best = {"artist": found_artist, "title": found_title,
                    "duration": None, "genre": genre_of(rec)}

    if best is None:
        return None

    # Durata: la mediana fra le registrazioni che corrispondono, non quella
    # della singola migliore. MusicBrainz cataloga anche radio edit e
    # anteprime, e per «Delilah» la corrispondenza più forte è un estratto da
    # 109 secondi contro i 221 del brano. Una durata canonica sbagliata sarebbe
    # peggio che nessuna: farebbe scartare proprio i candidati giusti.
    if durations:
        durations.sort()
        best["duration"] = durations[len(durations) // 2]

    return best


# ---------------------------------------------------------------------------
# Raccolta candidati
# ---------------------------------------------------------------------------

def _ytdlp_opts(with_cookies: bool = False) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "proxy": "", "ignoreerrors": True}
    if with_cookies:
        opts["cookiesfrombrowser"] = (COOKIE_BROWSER,)
    return opts


def _extract(url: str, with_cookies: bool = False) -> dict | None:
    import yt_dlp
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(with_cookies)) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception:
        return None


def _best_audio_format(info: dict) -> tuple[float | None, str]:
    """Bitrate e codec del formato audio migliore offerto dall'estrattore."""
    best_abr, best_codec = None, ""
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none"):
            continue
        abr = fmt.get("abr") or fmt.get("tbr")
        if abr and (best_abr is None or abr > best_abr):
            best_abr, best_codec = abr, fmt.get("acodec") or ""
    if best_abr is None:
        best_abr = info.get("abr")
        best_codec = info.get("acodec") or info.get("ext") or ""
    return best_abr, best_codec


def _candidates_from_search(prefix: str, query: str, source: str,
                            tier: int, limit: int) -> list[Candidate]:
    """Esegue una ricerca yt-dlp, es. `ytsearch5:artista titolo`."""
    import yt_dlp

    out: list[Candidate] = []
    opts = _ytdlp_opts()
    opts["extract_flat"] = False
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
    except Exception:
        return out

    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        abr, codec = _best_audio_format(entry)
        out.append(Candidate(
            url=entry.get("webpage_url") or entry.get("url") or "",
            title=entry.get("title") or "",
            uploader=entry.get("uploader") or entry.get("channel") or "",
            duration=entry.get("duration"),
            source=source, tier=tier, abr=abr, codec=codec,
        ))
    return [c for c in out if c.url]


def search_candidates(artist: str, title: str, limit: int = 5,
                      include_purchased: bool = True) -> list[Candidate]:
    """Raccoglie candidati da tutte le sorgenti abilitate."""
    query = f"{artist} {title}".strip()
    candidates: list[Candidate] = []

    if include_purchased:
        candidates += purchased_candidates(artist, title)

    candidates += _candidates_from_search("ytsearch", query, "YouTube", 3, limit)
    candidates += _candidates_from_search("scsearch", query, "SoundCloud", 4, limit)
    candidates += bandcamp_candidates(query)
    return candidates


# ---------------------------------------------------------------------------
# Store dove l'utente ha acquistato
# ---------------------------------------------------------------------------

def purchased_candidates(artist: str, title: str) -> list[Candidate]:
    """Cerca il brano fra gli acquisti dell'utente.

    Ogni adapter è indipendente e il suo fallimento non blocca gli altri: se i
    cookie sono scaduti o il sito ha cambiato pagina, si perde quella sorgente,
    non l'intero download.
    """
    found: list[Candidate] = []
    for adapter in (_bandcamp_collection, _beatport_downloads, _juno_downloads):
        try:
            found += adapter(artist, title)
        except Exception:
            continue
    return found


def _match(text: str, artist: str, title: str) -> bool:
    text = text.lower()
    return title.lower() in text and (not artist or artist.lower().split()[0] in text)


def _bandcamp_collection(artist: str, title: str) -> list[Candidate]:
    """Acquisti Bandcamp, letti dalla collezione con i cookie del browser.

    Bandcamp consegna FLAC dagli originali dell'artista: quando c'è, è la
    sorgente migliore in assoluto.
    """
    import yt_dlp

    username = os.environ.get("DJ_BANDCAMP_USER")
    if not username:
        return []

    opts = _ytdlp_opts(with_cookies=True)
    opts["extract_flat"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://bandcamp.com/{username}", download=False)
    except Exception:
        return []

    out = []
    for entry in (info or {}).get("entries") or []:
        if entry and _match(entry.get("title", ""), artist, title):
            out.append(Candidate(
                url=entry.get("url", ""), title=entry.get("title", ""),
                source="Bandcamp (acquistato)", tier=1, codec="flac",
            ))
    return [c for c in out if c.url]


def _store_downloads(base_url: str, pattern: str, source: str,
                     artist: str, title: str, env_key: str) -> list[Candidate]:
    """Legge una pagina "i miei download" di uno store usando i cookie del browser.

    Beatport e Juno non hanno un estrattore yt-dlp: si raschia la pagina degli
    acquisti e si prendono i link diretti ai file.
    """
    if not os.environ.get(env_key):
        return []
    try:
        import browser_cookie3  # opzionale
        import requests
    except ImportError:
        return []

    cookies = getattr(browser_cookie3, COOKIE_BROWSER)()
    response = requests.get(base_url, cookies=cookies, timeout=20)
    response.raise_for_status()

    out = []
    for match in re.finditer(pattern, response.text):
        url, label = match.group(1), match.group(0)
        if _match(label, artist, title):
            out.append(Candidate(url=url, title=title, source=source, tier=1, codec="wav"))
    return out


def _beatport_downloads(artist: str, title: str) -> list[Candidate]:
    return _store_downloads(
        "https://www.beatport.com/library/downloads",
        r'href="(https://[^"]*?/download[^"]*)"[^>]*>([^<]+)',
        "Beatport (acquistato)", artist, title, "DJ_BEATPORT_ENABLED")


def _juno_downloads(artist: str, title: str) -> list[Candidate]:
    return _store_downloads(
        "https://www.junodownload.com/myjuno/downloads/",
        r'href="(https://[^"]*?/download[^"]*)"[^>]*>([^<]+)',
        "Juno (acquistato)", artist, title, "DJ_JUNO_ENABLED")


def bandcamp_candidates(query: str) -> list[Candidate]:
    """Bandcamp pubblico: molti artisti offrono il download in FLAC."""
    try:
        import requests
    except ImportError:
        return []
    try:
        response = requests.get("https://bandcamp.com/search",
                                params={"q": query, "item_type": "t"}, timeout=15)
        response.raise_for_status()
    except Exception:
        return []

    urls = re.findall(r'href="(https://[^"]+\.bandcamp\.com/track/[^"?]+)', response.text)
    seen, out = set(), []
    for url in urls[:5]:
        if url in seen:
            continue
        seen.add(url)
        out.append(Candidate(url=url, title=query, source="Bandcamp", tier=2, codec="flac"))
    return out


# ---------------------------------------------------------------------------
# Valutazione e scelta
# ---------------------------------------------------------------------------

def score_candidate(candidate: Candidate, *, target_duration: float | None,
                    artist: str, title: str, allow_variants: bool = False,
                    deep: bool = True) -> float:
    """Assegna un punteggio a un candidato.

    Con `deep` si scarica una finestra di audio e se ne misura lo spettro: è
    l'unico modo per accorgersi che un "320 kbps" è in realtà un 128
    ricodificato. Costa qualche secondo per candidato, ed è il motivo per cui
    lo facciamo solo sui pochi che hanno superato i filtri grossolani.
    """
    score = 0.0
    notes: list[str] = []

    # Fiducia nella sorgente
    score += {1: 40.0, 2: 25.0, 3: 10.0, 4: 5.0}.get(candidate.tier, 0.0)

    # Durata: uno scarto grande significa quasi sempre versione diversa
    if target_duration and candidate.duration:
        delta = abs(candidate.duration - target_duration)
        if delta <= 2:
            score += 20.0
        elif delta <= 5:
            score += 10.0
        elif delta > 15:
            score -= 30.0
            notes.append(f"durata diversa di {delta:.0f} s dalla versione canonica")

    # Titolo
    haystack = f"{candidate.title} {candidate.uploader}".lower()
    if title and title.lower() in haystack:
        score += 10.0
    if artist and artist.lower().split()[0] in haystack:
        score += 5.0
    score += _similarity(f"{artist} {title}", f"{candidate.title} {candidate.uploader}") / 10
    if not allow_variants:
        for marker in _VARIANT_MARKERS:
            if marker in haystack and marker not in f"{artist} {title}".lower():
                score -= 25.0
                notes.append(f"sembra una versione «{marker}»")
                break

    # Bitrate dichiarato: indizio debole, ma gratuito
    if candidate.abr:
        score += min(10.0, candidate.abr / 320 * 10)

    # Misura vera dello spettro
    if deep:
        report = probe_candidate(candidate)
        if report is not None:
            candidate.report = report
            score += report.score * 0.5
            notes += report.notes

    candidate.notes = notes
    candidate.score = round(score, 1)
    return candidate.score


def probe_candidate(candidate: Candidate, seconds: int = 25) -> quality.QualityReport | None:
    """Scarica una finestra dal centro del brano e ne misura la qualità reale.

    Si prende dal centro perché intro e code sono spesso silenzio o fade, dove
    lo spettro non dice niente.

    La finestra si estrae passando l'URL del flusso direttamente a ffmpeg con
    `-ss`/`-t`, invece di usare il download a spezzoni di yt-dlp: quest'ultimo
    ricodifica e fallisce su parecchi formati, e in ogni caso ricodificare
    distruggerebbe proprio il taglio spettrale che vogliamo misurare. Con
    `-c copy` i byte arrivano come sono sul server.
    """
    import yt_dlp

    if not shutil.which("ffmpeg"):
        return None

    info = _extract(candidate.url, with_cookies=(candidate.tier == 1))
    if not info:
        return None

    stream_url, headers = _best_stream(info)
    if not stream_url:
        return None

    start = max(0.0, (candidate.duration or info.get("duration") or 120.0) / 2 - seconds / 2)
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "probe.mka"
        command = ["ffmpeg", "-v", "error", "-y"]
        if headers:
            command += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        command += ["-ss", f"{start:.2f}", "-t", str(seconds), "-i", stream_url,
                    "-vn", "-c:a", "copy", str(target)]
        try:
            proc = subprocess.run(command, capture_output=True, timeout=120)
        except subprocess.SubprocessError:
            return None
        if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1024:
            return None

        report = quality.assess(target)

    # Il contenitore temporaneo non dice nulla sul codec di origine: si tengono
    # i dati dichiarati dall'estrattore, e dalla misura solo lo spettro.
    if candidate.codec:
        report.codec = candidate.codec.split(".")[0]
    if candidate.abr:
        report.bitrate_kbps = int(candidate.abr)
    report.lossless_container = report.codec in quality._LOSSLESS_CODECS
    report.score = quality._score(report)
    return report


def _best_stream(info: dict) -> tuple[str, dict]:
    """URL diretto e header del miglior formato solo-audio."""
    best, best_abr = None, -1.0
    for fmt in info.get("formats") or []:
        if fmt.get("vcodec") not in (None, "none") or not fmt.get("url"):
            continue
        abr = fmt.get("abr") or fmt.get("tbr") or 0
        if abr > best_abr:
            best, best_abr = fmt, abr
    if best is None:
        return info.get("url", ""), info.get("http_headers") or {}
    return best["url"], best.get("http_headers") or {}


def choose(candidates: list[Candidate], *, target_duration: float | None,
           artist: str, title: str, allow_variants: bool = False,
           deep_probe: int = 3) -> Candidate | None:
    """Sceglie il candidato migliore.

    Si valutano tutti in modo grossolano, poi si misura davvero lo spettro solo
    ai primi `deep_probe`: la misura richiede di scaricare audio, e farla su
    venti candidati costerebbe più del download vero.
    """
    if not candidates:
        return None

    for candidate in candidates:
        score_candidate(candidate, target_duration=target_duration, artist=artist,
                        title=title, allow_variants=allow_variants, deep=False)
    candidates.sort(key=lambda c: c.score, reverse=True)

    for candidate in candidates[:deep_probe]:
        score_candidate(candidate, target_duration=target_duration, artist=artist,
                        title=title, allow_variants=allow_variants, deep=True)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[0]


def candidate_for_url(url: str) -> Candidate | None:
    """Costruisce un candidato a partire da un URL fornito dall'utente."""
    info = _extract(url)
    if not info:
        return None
    abr, codec = _best_audio_format(info)

    lowered = url.lower()
    if "bandcamp" in lowered:
        source, tier = "Bandcamp", 2
    elif "soundcloud" in lowered:
        source, tier = "SoundCloud", 4
    elif "youtu" in lowered:
        source, tier = "YouTube", 3
    else:
        source, tier = "Web", 5

    return Candidate(
        url=url,
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        source=source, tier=tier, abr=abr, codec=codec,
    )

# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

def is_playlist(url: str) -> bool:
    """Riconosce gli URL che contengono più di una traccia."""
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        return False
    return any(marker in lowered for marker in (
        "list=", "/playlist", "/album", "/sets/", "/set/", "/tracks",
        "/releases", "/discography", "/channel/", "/@",
    ))


def expand_playlist(url: str, limit: int = 500) -> list[str]:
    """Espande una playlist nell'elenco delle tracce da preparare.

    Per Spotify si restituiscono «artista - titolo», non URL: da Spotify non si
    scarica audio, quindi ogni brano va comunque cercato altrove — ed è
    esattamente il percorso che vogliamo, perché passa dalla scelta della
    sorgente migliore invece di accettare la prima disponibile.

    Per le altre piattaforme si restituiscono gli URL dei singoli brani, che
    conservano l'informazione su quale versione l'utente intendeva.
    """
    if "spotify" in url.lower():
        return _expand_spotify(url, limit)
    return _expand_ytdlp(url, limit)


def _expand_ytdlp(url: str, limit: int) -> list[str]:
    import yt_dlp

    opts = _ytdlp_opts()
    # extract_flat: si vuole solo l'elenco, non i dettagli di ogni brano —
    # risolverli tutti su una playlist da 200 pezzi richiederebbe minuti.
    opts["extract_flat"] = "in_playlist"
    opts["playlistend"] = limit
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return []

    out = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        target = entry.get("url") or entry.get("webpage_url")
        if target:
            out.append(target)
    return out


def _expand_spotify(url: str, limit: int) -> list[str]:
    """Elenco dei brani di una playlist Spotify, come «artista - titolo».

    Si usa `spotdl save`, che scrive i soli metadata senza scaricare nulla:
    l'audio lo prenderemo dalla sorgente migliore che troviamo, non da Spotify.
    """
    import json
    import sys
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "playlist.spotdl"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "spotdl", "save", url, "--save-file", str(target)],
                capture_output=True, text=True, timeout=300,
                env={k: v for k, v in os.environ.items()
                     if k.lower() not in ("https_proxy", "http_proxy", "all_proxy")},
            )
        except subprocess.SubprocessError:
            return []
        if not target.exists():
            return []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    out = []
    for song in data[:limit]:
        artist = song.get("artist") or ""
        name = song.get("name") or ""
        if name:
            out.append(f"{artist} - {name}".strip(" -"))
    return out


# ---------------------------------------------------------------------------
# Genere
# ---------------------------------------------------------------------------

# Etichette troppo ampie per essere utili a un DJ: se un artista è taggato sia
# «electronic» sia «house» con lo stesso peso, quella che serve è la seconda.
_GENERIC_GENRES = {
    "electronic", "electronica", "edm", "dance", "club", "music",
    "pop", "rock", "alternative", "indie", "experimental", "instrumental",
}

# I generi si chiedono per artista, non per brano: le registrazioni su
# MusicBrainz sono quasi sempre senza tag, mentre gli artisti no. Una cache in
# memoria evita di ripetere la richiesta per ogni traccia dello stesso artista,
# che su un album intero sarebbe una richiesta per pezzo.
_genre_cache: dict[str, str] = {}


def artist_genre(artist_id: str) -> str:
    """Genere prevalente di un artista secondo i tag di MusicBrainz."""
    if not artist_id:
        return ""
    if artist_id in _genre_cache:
        return _genre_cache[artist_id]

    try:
        import musicbrainzngs
        artist = musicbrainzngs.get_artist_by_id(artist_id, includes=["tags"])["artist"]
    except Exception:
        _genre_cache[artist_id] = ""
        return ""

    candidates = []
    for tag in artist.get("tag-list") or []:
        try:
            count = int(tag.get("count", 0))
        except (TypeError, ValueError):
            continue
        name = (tag.get("name") or "").strip().lower()
        # Un tag con un voto solo è più spesso un'opinione che una
        # classificazione: non basta a spostare una traccia in una cartella.
        if name and count >= 2:
            candidates.append((count, name not in _GENERIC_GENRES, name))

    genre = max(candidates)[2].title() if candidates else ""
    _genre_cache[artist_id] = genre
    return genre

#!/usr/bin/env python3
"""Web frontend per edi.audio — download e preparazione DJ delle tracce."""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, request, send_file

import core
from dj import analysis, export, mixxx, pipeline

app = Flask(__name__, static_folder=".", static_url_path="")

PENDING  = "⏳ in attesa"
RUNNING  = "⬇ in corso"
DONE     = "✅ pronta"
ERROR    = "❌ errore"

# Cartella della libreria DJ. Sta fuori dal progetto perché è musica, non
# codice, e perché Mixxx deve poterla tenere nella propria libreria.
DEFAULT_OUTPUT = str(Path.home() / "Musica" / "DJ")

items: list[dict] = []
msg_queue: queue.Queue = queue.Queue()
downloading = False
last_export: dict | None = None


# ── API ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("index.html")


@app.route("/api/queue", methods=["GET"])
def get_queue():
    return jsonify([_public(i) for i in items])


@app.route("/api/queue", methods=["POST"])
def add_to_queue():
    data = request.get_json(silent=True) or {}
    entry = (data.get("url") or "").strip()
    if not entry:
        return jsonify({"error": "URL o titolo richiesto"}), 400

    is_url = entry.startswith(("http://", "https://"))
    if is_url and core.is_downloaded(entry):
        return jsonify({"error": "Già scaricato", "duplicate": True}), 409

    if not is_url:
        label = "Ricerca per titolo"
    elif pipeline.sources.is_playlist(entry):
        label = f"Playlist {_source_label(entry)}"
    else:
        label = _source_label(entry)

    items.append({
        "url": entry,
        "status": PENDING,
        "label": label,
        "phase": "",
        "message": "",
        "result": None,
    })
    return jsonify({"ok": True, "queue": [i["url"] for i in items]}), 201


@app.route("/api/queue/<int:idx>", methods=["DELETE"])
def remove_from_queue(idx):
    if not downloading and 0 <= idx < len(items):
        items.pop(idx)
    return jsonify({"ok": True})


@app.route("/api/queue", methods=["DELETE"])
def clear_queue():
    if not downloading:
        items.clear()
    return jsonify({"ok": True})


@app.route("/api/history")
def get_history():
    return jsonify(core.load_history())


@app.route("/api/capabilities")
def get_capabilities():
    """Cosa può fare davvero questa installazione.

    La UI la interroga all'avvio per non promettere BPM e cue quando il venv
    di analisi non è installato.
    """
    status = mixxx.roundtrip_status()
    return jsonify({
        "ffmpeg": core.check_ffmpeg(),
        "analysis": analysis.is_available(),
        "analysis_hint": "esegui ./setup_analysis_venv.sh per abilitare BPM, chiave e cue",
        "workers": pipeline.default_workers(),
        "output_dir": DEFAULT_OUTPUT,
        "mixxx_installed": status["installed"],
        "mixxx_roundtrip": status["ok"],
        "mixxx_hint": status.get("reason", ""),
        "mixxx_steps": status.get("steps", []),
    })


@app.route("/api/download", methods=["POST"])
def start_downloads():
    """Avvia la pipeline completa sulla coda."""
    global downloading
    if downloading:
        return jsonify({"error": "Download già in corso"}), 409

    data = request.get_json(silent=True) or {}
    output_dir = data.get("output_dir", DEFAULT_OUTPUT)
    search_alternatives = bool(data.get("search_alternatives", True))
    allow_variants = bool(data.get("allow_variants", False))

    if not any(it["status"] in (PENDING, ERROR) for it in items):
        return jsonify({"error": "Coda vuota"}), 400

    downloading = True
    threading.Thread(
        target=_worker,
        args=(output_dir, search_alternatives, allow_variants),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/queue/expand", methods=["POST"])
def expand_queue():
    """Sostituisce le playlist in coda con le tracce che contengono."""
    if downloading:
        return jsonify({"error": "operazione in corso"}), 409

    # force: l'utente ha premuto il pulsante apposta, quindi vale la pena
    # tentare anche sugli indirizzi che l'euristica non riconosce
    added = _expand_pending(force=True)
    return jsonify({"ok": True, "added": added, "queue": len(items)})


@app.route("/api/prepare", methods=["POST"])
def prepare_existing():
    """Prepara un file già presente sul disco, senza riscaricarlo."""
    data = request.get_json(silent=True) or {}
    path = data.get("path")
    if not path or not Path(path).is_file():
        return jsonify({"error": "percorso non valido"}), 400

    result = pipeline.prepare_file(path)
    return jsonify(_result_dict(result)), (200 if result.ok else 500)


@app.route("/api/tracks")
def get_tracks():
    """Libreria preparata, letta dai tag dei file."""
    directory = request.args.get("dir", DEFAULT_OUTPUT)
    if not Path(directory).is_dir():
        return jsonify([])
    return jsonify(export.scan(directory))


@app.route("/api/export", methods=["POST"])
def do_export():
    """Rigenera playlist M3U8 e rekordbox.xml."""
    global last_export
    data = request.get_json(silent=True) or {}
    directory = data.get("dir", DEFAULT_OUTPUT)
    organize = bool(data.get("organize", False))
    if not Path(directory).is_dir():
        return jsonify({"error": "cartella inesistente"}), 400

    last_export = export.export_all(directory, organize=organize)
    return jsonify(last_export)


@app.route("/api/rescan", methods=["POST"])
def do_rescan():
    """Prepara i FLAC già presenti in una cartella."""
    global downloading
    if downloading:
        return jsonify({"error": "operazione già in corso"}), 409

    data = request.get_json(silent=True) or {}
    directory = data.get("dir", DEFAULT_OUTPUT)
    if not Path(directory).is_dir():
        return jsonify({"error": "cartella inesistente"}), 400

    downloading = True

    def run():
        global downloading
        try:
            results = pipeline.rescan(
                directory,
                progress=lambda phase, message="": msg_queue.put(("phase", phase, message)),
            )
            msg_queue.put(("info", f"{sum(1 for r in results if r.ok)}/{len(results)} tracce preparate"))
        except Exception as exc:
            msg_queue.put(("error", str(exc)[:200]))
        finally:
            downloading = False
            msg_queue.put(("done", None))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/status")
def get_status():
    messages = []
    try:
        while True:
            messages.append(msg_queue.get_nowait())
    except queue.Empty:
        pass
    return jsonify({
        "downloading": downloading,
        "items": [_public(i) for i in items],
        "messages": messages,
        "last_export": last_export,
    })


# ── Internals ────────────────────────────────────────────────────────────

def _public(item: dict) -> dict:
    """Vista dell'elemento esposta alla UI."""
    return {
        "url": item["url"],
        "status": item["status"],
        "label": item["label"],
        "phase": item.get("phase", ""),
        "message": item.get("message", ""),
        "result": item.get("result"),
    }


def _result_dict(result) -> dict:
    return {
        "ok": result.ok,
        "path": result.path,
        "artist": result.artist,
        "title": result.title,
        "bpm": result.bpm,
        "key": result.key,
        "camelot": result.camelot,
        "quality": result.quality_label,
        "quality_score": result.quality_score,
        "source": result.source,
        "cues": result.cues,
        "loops": result.loops,
        "preserved": result.preserved,
        "notes": result.notes,
        "error": result.error,
    }


def _source_label(url):
    lowered = url.lower()
    if "spotify" in lowered:
        return "Spotify"
    if "youtube" in lowered or "youtu.be" in lowered:
        return "YouTube"
    if "soundcloud" in lowered:
        return "SoundCloud"
    if "bandcamp" in lowered:
        return "Bandcamp"
    return "Web"


def _prepare_one(item, output_dir, search_alternatives, allow_variants):
    item["status"] = RUNNING

    def progress(phase, message="", _item=item):
        # La fase per-traccia è quello che rende leggibile l'attesa: l'analisi
        # è lenta e senza saperlo sembra che l'app sia bloccata.
        _item["phase"] = phase
        _item["message"] = message
        msg_queue.put(("phase", phase, f"{_item['url'][:40]} — {message}" if message else phase))

    try:
        result = pipeline.prepare(
            item["url"], output_dir,
            progress=progress,
            search_alternatives=search_alternatives,
            allow_variants=allow_variants,
        )
        item["result"] = _result_dict(result)
        item["status"] = DONE if result.ok else ERROR
        if not result.ok:
            msg_queue.put(("error", result.error[:200]))
    except Exception as exc:
        item["status"] = ERROR
        item["message"] = str(exc).splitlines()[0][:160]
        msg_queue.put(("error", item["message"]))


def _expand_pending(force: bool = False) -> int:
    """Sostituisce le playlist in coda con le tracce che contengono.

    Si fa qui e non dentro la richiesta HTTP perché per una playlist Spotify
    significa interrogare la rete per una decina di secondi, e la pagina
    resterebbe bloccata ad aspettare.

    Con `force` si tenta l'espansione su qualunque URL invece che solo su
    quelli che *sembrano* una playlist. È il comportamento giusto quando è
    l'utente a premere il pulsante — riconoscere una playlist dall'indirizzo
    funziona solo per i formati che qualcuno ha già previsto — ma non quando
    l'espansione parte da sola: là costerebbe un'interrogazione di rete in più
    per ogni singolo brano della coda.
    """
    added = 0
    for index in reversed(range(len(items))):
        entry = items[index]
        if entry["status"] not in (PENDING, ERROR):
            continue
        if not entry["url"].startswith(("http://", "https://")):
            continue
        if not force and not pipeline.sources.is_playlist(entry["url"]):
            continue

        msg_queue.put(("phase", "coda", f"espando {entry['url'][:60]}"))
        tracks = pipeline.sources.expand_playlist(entry["url"])
        # Una sola traccia significa che non era una playlist: si lascia com'è
        if len(tracks) < 2:
            continue

        items.pop(index)
        for track in reversed(tracks):
            items.insert(index, {
                "url": track,
                "status": PENDING,
                "label": _source_label(track) if track.startswith("http") else "Ricerca per titolo",
                "phase": "", "message": "", "result": None,
            })
        added += len(tracks)
    return added


def _worker(output_dir, search_alternatives, allow_variants):
    """Prepara la coda, più tracce alla volta.

    Le playlist si espandono da sole prima di partire: chiedere all'utente di
    ricordarsi di premere un pulsante prima di un altro è un modo garantito per
    far fallire il lavoro, e il fallimento sarebbe pure oscuro («nessuna
    sorgente trovata» per quello che è un elenco, non un brano).

    Il parallelismo conta quanto nella CLI: l'analisi occupa un core per circa
    un sesto della durata del brano, quindi una coda di venti pezzi in seriale
    sono venti minuti buoni di attesa.
    """
    global downloading

    try:
        added = _expand_pending()
        if added:
            msg_queue.put(("info", f"{added} tracce estratte dalle playlist"))

        pending = [it for it in items if it["status"] in (PENDING, ERROR)]
        workers = pipeline.default_workers()
        msg_queue.put(("phase", "coda", f"{len(pending)} tracce, {workers} in parallelo"))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(
                lambda item: _prepare_one(item, output_dir,
                                          search_alternatives, allow_variants),
                pending,
            ))
    except Exception as exc:
        msg_queue.put(("error", str(exc).splitlines()[0][:200]))
    finally:
        downloading = False
        msg_queue.put(("done", None))


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    if not analysis.is_available():
        print("  ⚠  venv di analisi assente: esegui ./setup_analysis_venv.sh")
        print("     senza, le tracce si scaricano ma restano senza BPM, chiave e cue")
    print(f"  →  http://{host}:{port}")
    app.run(host=host, port=port, debug=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""DJ Downloader — app desktop per scaricare tracce per DJ set.

Supporta YouTube, SoundCloud, Bandcamp (via yt-dlp) e Spotify (via spotdl).
Coda multipla, metadata/copertina, cronologia con rilevamento doppioni.
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import core

PENDING  = "⏳ in attesa"
RUNNING  = "⬇ in corso"
DONE     = "✅ fatto"
ERROR    = "❌ errore"

ICON_SPOTIFY = "🎵"
ICON_WEB     = "▶"


def _source_label(url):
    url = url.lower()
    if "spotify" in url:
        return f"{ICON_SPOTIFY} Spotify"
    if "youtube" in url or "youtu.be" in url:
        return f"{ICON_WEB} YouTube"
    if "soundcloud" in url:
        return f"{ICON_WEB} SoundCloud"
    if "bandcamp" in url:
        return f"{ICON_WEB} Bandcamp"
    return f"{ICON_WEB} Web"


class DownloaderApp:
    def __init__(self, root):
        self.root = root
        root.title("DJ Downloader")
        root.geometry("780x680")
        root.minsize(660, 560)

        self.items = []           # [{url, status, label}]
        self.ui_queue = queue.Queue()
        self.downloading = False

        self._build_ui()
        self._refresh_history()
        self._poll_ui_queue()

        if not core.check_ffmpeg():
            messagebox.showwarning(
                "ffmpeg mancante",
                "ffmpeg non è nel PATH — i download falliranno.\n\n"
                "Installa con:\n"
                "  Linux:  sudo apt install ffmpeg\n"
                "  macOS:  brew install ffmpeg\n"
                "  Win:    https://ffmpeg.org/download.html",
            )

    # -----------------------------------------------------------------------
    # UI
    # -----------------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 10, "pady": 5}

        # Titolo
        ttk.Label(self.root, text="DJ Downloader",
                  font=("", 16, "bold")).pack(pady=(12, 0))
        ttk.Label(self.root,
                  text="YouTube · SoundCloud · Bandcamp · Spotify",
                  foreground="gray").pack(pady=(0, 8))

        # Input link
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Link:").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.url_entry.bind("<Return>", lambda _: self._add_to_queue())
        ttk.Button(top, text="+ Aggiungi alla coda",
                   command=self._add_to_queue).pack(side="left")

        # Coda
        qframe = ttk.LabelFrame(self.root, text="Coda download")
        qframe.pack(fill="both", expand=True, **pad)

        list_frame = ttk.Frame(qframe)
        list_frame.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        scroll = ttk.Scrollbar(list_frame, orient="vertical")
        self.queue_list = tk.Listbox(list_frame, height=8,
                                     yscrollcommand=scroll.set,
                                     selectmode="browse")
        scroll.config(command=self.queue_list.yview)
        self.queue_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        qbtns = ttk.Frame(qframe)
        qbtns.pack(side="right", fill="y", padx=6, pady=6)
        ttk.Button(qbtns, text="Rimuovi", command=self._remove_selected).pack(fill="x", pady=2)
        ttk.Button(qbtns, text="Svuota coda", command=self._clear_queue).pack(fill="x", pady=2)

        # Opzioni
        opts_frame = ttk.LabelFrame(self.root, text="Formato")
        opts_frame.pack(fill="x", **pad)
        self.fmt_var = tk.StringVar(value=core.DEFAULT_FORMAT)
        ttk.Radiobutton(opts_frame, text="FLAC  (per DJ: griglia e cue)",
                        value="flac", variable=self.fmt_var,
                        command=self._toggle_quality).pack(side="left", padx=10, pady=6)
        ttk.Radiobutton(opts_frame, text="MP3  (compatibilità)",
                        value="mp3", variable=self.fmt_var,
                        command=self._toggle_quality).pack(side="left", padx=10, pady=6)
        ttk.Label(opts_frame, text="kbps:").pack(side="left", padx=(20, 2))
        self.quality_var = tk.StringVar(value="320")
        self.quality_combo = ttk.Combobox(opts_frame, textvariable=self.quality_var,
                                           width=5, state="readonly",
                                           values=["320", "256", "192"])
        self.quality_combo.pack(side="left", pady=6)

        # Cartella
        dest = ttk.Frame(self.root)
        dest.pack(fill="x", **pad)
        ttk.Label(dest, text="Cartella:").pack(side="left")
        self.dir_var = tk.StringVar(value=os.path.abspath("./downloads"))
        ttk.Entry(dest, textvariable=self.dir_var).pack(side="left", fill="x",
                                                         expand=True, padx=6)
        ttk.Button(dest, text="Sfoglia …", command=self._browse_dir).pack(side="left")

        # Azione + barra
        action = ttk.Frame(self.root)
        action.pack(fill="x", **pad)
        self.download_btn = ttk.Button(action, text="⬇  Scarica tutto",
                                       command=self._start_downloads)
        self.download_btn.pack(side="left")
        self.progress = ttk.Progressbar(action, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=10)

        # Stato
        self.status_var = tk.StringVar(value="Pronto.")
        status_lbl = ttk.Label(self.root, textvariable=self.status_var,
                                anchor="w", foreground="gray")
        status_lbl.pack(fill="x", padx=10, pady=(0, 4))

        # Cronologia
        hframe = ttk.LabelFrame(self.root, text="Cronologia")
        hframe.pack(fill="both", expand=True, **pad)

        cols = ("title", "source", "format", "timestamp")
        self.history_tree = ttk.Treeview(hframe, columns=cols,
                                          show="headings", height=5)
        self.history_tree.heading("title",     text="Titolo / URL")
        self.history_tree.heading("source",    text="Sorgente")
        self.history_tree.heading("format",    text="Formato")
        self.history_tree.heading("timestamp", text="Data")
        self.history_tree.column("source",    width=90,  anchor="center")
        self.history_tree.column("format",    width=70,  anchor="center")
        self.history_tree.column("timestamp", width=140, anchor="center")

        hs = ttk.Scrollbar(hframe, orient="vertical",
                           command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=hs.set)
        self.history_tree.pack(side="left", fill="both", expand=True,
                                padx=6, pady=6)
        hs.pack(side="right", fill="y", pady=6)

    # -----------------------------------------------------------------------
    # Gestione coda
    # -----------------------------------------------------------------------

    def _add_to_queue(self):
        url = self.url_var.get().strip()
        if not url:
            return
        if core.is_downloaded(url):
            if not messagebox.askyesno(
                    "Già scaricato",
                    "Questo link è già nella cronologia.\nScaricarlo di nuovo?"):
                return
        label = f"{_source_label(url)}  —  {url}"
        self.items.append({"url": url, "status": PENDING, "label": label})
        self.queue_list.insert("end", f"{PENDING}  {label}")
        self.url_var.set("")
        self.url_entry.focus()

    def _remove_selected(self):
        if self.downloading:
            return
        sel = self.queue_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.queue_list.delete(idx)
        del self.items[idx]

    def _clear_queue(self):
        if self.downloading:
            return
        self.queue_list.delete(0, "end")
        self.items.clear()

    def _toggle_quality(self):
        state = "readonly" if self.fmt_var.get() == "mp3" else "disabled"
        self.quality_combo.configure(state=state)

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.getcwd())
        if d:
            self.dir_var.set(d)

    def _set_item_status(self, idx, status):
        if idx >= len(self.items):
            return
        self.items[idx]["status"] = status
        self.queue_list.delete(idx)
        self.queue_list.insert(idx, f"{status}  {self.items[idx]['label']}")

    # -----------------------------------------------------------------------
    # Download (thread worker)
    # -----------------------------------------------------------------------

    def _start_downloads(self):
        if self.downloading:
            return
        pending = [i for i, it in enumerate(self.items)
                   if it["status"] in (PENDING, ERROR)]
        if not pending:
            messagebox.showinfo("Coda vuota",
                                "Aggiungi almeno un link prima di scaricare.")
            return
        if not core.check_ffmpeg():
            messagebox.showerror("ffmpeg mancante",
                                 "Installa ffmpeg per poter scaricare.")
            return

        self.downloading = True
        self.download_btn.configure(state="disabled")

        fmt        = self.fmt_var.get()
        quality    = int(self.quality_var.get())
        output_dir = self.dir_var.get().strip() or os.path.abspath("./downloads")

        t = threading.Thread(
            target=self._worker,
            args=(pending, fmt, output_dir, quality),
            daemon=True,
        )
        t.start()

    def _worker(self, indices, fmt, output_dir, quality):
        total = len(indices)

        for n, idx in enumerate(indices, 1):
            url = self.items[idx]["url"]
            self.ui_queue.put(("status_item", idx, RUNNING))
            self.ui_queue.put(("status", f"[{n}/{total}] {_source_label(url)}  {url}"))
            self.ui_queue.put(("progress_mode", "indeterminate"))

            # progress hook per yt-dlp (progress in %)
            def ytdlp_hook(d, _idx=idx):
                if d["status"] == "downloading":
                    total_b = d.get("total_bytes") or d.get("total_bytes_estimate")
                    done_b  = d.get("downloaded_bytes", 0)
                    if total_b:
                        pct = done_b / total_b * 100
                        self.ui_queue.put(("progress_set", pct))
                        spd = d.get("_speed_str", "").strip()
                        self.ui_queue.put(("status",
                            f"[{n}/{total}] {d.get('filename', '').split(os.sep)[-1]}  "
                            f"{pct:.0f}%  {spd}"))
                elif d["status"] == "finished":
                    self.ui_queue.put(("progress_set", 100))

            # callback per spotdl (righe di testo)
            def spotdl_line(line, _n=n, _total=total):
                # Filtra righe di log yt-dlp interne a spotdl (rumore)
                if any(x in line for x in ("[debug]", "WARNING:", "[ffmpeg]")):
                    return
                self.ui_queue.put(("status", f"[{_n}/{_total}] {line}"))

            try:
                core.download_one(
                    url, fmt, output_dir, quality,
                    progress_hook=ytdlp_hook,
                    on_line=spotdl_line,
                )
                self.ui_queue.put(("status_item", idx, DONE))
            except Exception as exc:
                self.ui_queue.put(("status_item", idx, ERROR))
                # Mostra solo la prima riga del messaggio di errore nella barra
                first_line = str(exc).splitlines()[0][:120]
                self.ui_queue.put(("status", f"❌ Errore [{n}/{total}]: {first_line}"))

        self.ui_queue.put(("done", None))

    def _poll_ui_queue(self):
        """Svuota la coda messaggi dal thread — chiamato ogni 80 ms da Tk."""
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                kind = msg[0]

                if kind == "status_item":
                    self._set_item_status(msg[1], msg[2])

                elif kind == "progress_mode":
                    mode = msg[1]
                    self.progress.configure(mode=mode)
                    if mode == "indeterminate":
                        self.progress.start(12)

                elif kind == "progress_set":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = msg[1]

                elif kind == "status":
                    self.status_var.set(msg[1])

                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress["value"] = 0
                    self.downloading = False
                    self.download_btn.configure(state="normal")
                    self.status_var.set("✅ Tutti i download completati.")
                    self._refresh_history()

        except queue.Empty:
            pass
        self.root.after(80, self._poll_ui_queue)

    # -----------------------------------------------------------------------
    # Cronologia
    # -----------------------------------------------------------------------

    def _refresh_history(self):
        for row in self.history_tree.get_children():
            self.history_tree.delete(row)
        for entry in reversed(core.load_history()):
            src = entry.get("source", "ytdlp")
            src_label = f"{ICON_SPOTIFY} Spotify" if src == "spotify" else f"{ICON_WEB} Web"
            self.history_tree.insert("", "end", values=(
                entry.get("title", entry.get("url", "")),
                src_label,
                entry.get("format", "").upper(),
                entry.get("timestamp", ""),
            ))


def main():
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

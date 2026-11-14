"""Tkinter interface. Network threads NEVER touch widgets: they leave messages in a queue
that the main thread drains with `poll()`."""
import base64
import csv
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import APP_NAME, __version__, net, settings, sources
from .merge import (FILTERS, SORT_LABELS, Store, order_items, passes, query_words, relevance,
                    safe_name, sorted_files)
from .models import ALL_LANGS, LANGS, isbn_of, safe_url

SEARCH_DEADLINE = 45  # max seconds to wait for the sources (per batch)
WIDEN_BELOW = 40      # with fewer books than this, the search widens itself
HEAD = {"title": "Title", "author": "Author", "year": "Year", "lang": "Language", "fmt": "Formats", "src": "Sources"}
log = settings.get_logger()


def open_path(path):
    """Open a file or folder with the system's default program."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        log.warning("open %s: %r", path, e)


def reveal_file(path):
    """Open the folder and select the file (Windows Explorer / Finder)."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as e:
        log.warning("reveal %s: %r", path, e)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = settings.load_settings()
        net.STATE.update({k: self.cfg[k] for k in ("google_key", "contact", "europeana_key", "core_key")})
        self.title("%s %s – Free books" % (APP_NAME, __version__))
        self.geometry("1080x690")
        self.minsize(980, 520)
        self._set_icon()

        self.msgs = queue.Queue()
        self.store, self.iids, self.gen = Store(), {}, 0
        self.fmt_map = {}
        self.sort_col, self.sort_desc, self.qwords = "rel", False, []
        self.loans = tk.BooleanVar(value=self.cfg["loans"])
        # search
        self.searching, self.search_cancel, self.search_started = False, None, 0.0
        self.search_total, self.search_done, self.search_failed = 0, set(), []
        self.query, self.isbn, self.page, self.wave = "", None, 0, 0
        self.can_widen, self.widened, self.failed_all = False, False, []
        self.resort_dirty, self.last_resort = False, 0.0
        # download
        self.downloading, self.dl_cancel, self.last_file = False, None, None
        self._last_prog = 0.0

        self._setup_fonts()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Escape>", lambda e: self.cancel_search())
        self.bind("<Control-l>", lambda e: (self.entry.focus(), self.entry.selection_range(0, "end")))
        self.after(100, self.poll)

    # ---------------- construction ----------------
    def _set_icon(self):
        try:
            from .icon import ICON_PNG_B64
            self._icon = tk.PhotoImage(data=base64.b64decode(ICON_PNG_B64))
            self.iconphoto(True, self._icon)
        except Exception as e:
            log.info("no window icon: %r", e)

    def _setup_fonts(self):
        fams = set(tkfont.families(self))
        base = "Segoe UI" if "Segoe UI" in fams else tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_ui, self.f_entry = (base, 10), (base, 12)
        style = ttk.Style(self)
        linespace = tkfont.Font(font=self.f_ui).metrics("linespace")
        style.configure("Treeview", rowheight=max(22, linespace + 8), font=self.f_ui)  # sharp on HiDPI screens
        style.configure("Treeview.Heading", font=self.f_ui)

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        self.entry = ttk.Combobox(top, font=self.f_entry, values=self.cfg["history"])
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.bind("<Return>", lambda e: self.start())
        self.entry.bind("<<ComboboxSelected>>", lambda e: self.start())
        self.entry.focus()
        self.lang_box = ttk.Combobox(top, state="readonly", width=16, font=self.f_ui,
                                     values=list(LANGS) + [ALL_LANGS])
        self.lang_box.set(self.cfg["lang"] if self.cfg["lang"] in list(LANGS) + [ALL_LANGS] else "English")
        self.lang_box.pack(side="left", padx=(8, 0), ipady=3)
        self.lang_box.bind("<<ComboboxSelected>>", lambda e: self.rerun())
        self.btn = ttk.Button(top, text="Search", width=10, command=self.on_search_btn)
        self.btn.pack(side="left", padx=(8, 0))

        bar = ttk.Frame(self, padding=(10, 0, 10, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="Sort by:").pack(side="left")
        self.sort_box = ttk.Combobox(bar, state="readonly", width=14, values=list(SORT_LABELS))
        self.sort_box.set("Relevance")
        self.sort_box.pack(side="left", padx=6)
        self.sort_box.bind("<<ComboboxSelected>>", lambda e: self.sort_by(SORT_LABELS[self.sort_box.get()], keep=True))
        self.dir_btn = ttk.Button(bar, text="A → Z", width=13, command=self.toggle_dir)
        self.dir_btn.pack(side="left")
        ttk.Label(bar, text="Filter:").pack(side="left", padx=(12, 0))
        self.filter_box = ttk.Combobox(bar, state="readonly", width=10, values=list(FILTERS))
        self.filter_box.set(self.cfg["filter"] if self.cfg["filter"] in FILTERS else "Flexible")
        self.filter_box.pack(side="left", padx=6)
        self.filter_box.bind("<<ComboboxSelected>>", lambda e: self.rerun())
        ttk.Checkbutton(bar, text="Include free loans", variable=self.loans,
                        command=self.rerun).pack(side="left", padx=8)
        self.more_btn = ttk.Button(bar, text="＋ More results", command=self.load_more, state="disabled")
        self.more_btn.pack(side="left", padx=4)

        more = ttk.Menubutton(bar, text="☰ More")
        mm = tk.Menu(more, tearoff=0)
        mm.add_command(label="Export results to CSV…", command=self.export_csv)
        mm.add_command(label="View error log", command=lambda: open_path(settings.log_path()))
        mm.add_command(label="Open data folder", command=lambda: open_path(settings.data_dir()))
        mm.add_separator()
        mm.add_command(label="About…", command=self.about)
        more["menu"] = mm
        more.pack(side="right")
        ttk.Button(bar, text="⚙ Settings", command=self.open_settings).pack(side="right", padx=6)
        self.web_btn = ttk.Menubutton(bar, text="Also search the web ▾")
        self.menu = tk.Menu(self.web_btn, tearoff=0)
        self.web_btn["menu"] = self.menu
        self.web_btn.pack(side="right")
        self.rebuild_menu()

        mid = ttk.Frame(self, padding=(10, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=tuple(HEAD), show="headings", selectmode="browse")
        for c, w in (("title", 320), ("author", 180), ("year", 55), ("lang", 60), ("fmt", 120), ("src", 190)):
            self.tree.heading(c, text=HEAD[c], command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, anchor="center" if c in ("year", "lang") else "w")
        self.tree.tag_configure("webonly", foreground="#7a7a7a")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        self.tree.bind("<Double-1>", lambda e: self.activate())
        self.tree.bind("<Return>", lambda e: self.activate())
        for ev in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.tree.bind(ev, self.popup)
        self.ctx = tk.Menu(self, tearoff=0)
        self.ctx.add_command(label="Download", command=self.start_download)
        self.ctx.add_command(label="Open page", command=self.open_page)
        self.ctx.add_separator()
        self.ctx.add_command(label="Copy page link", command=lambda: self.copy("page"))
        self.ctx.add_command(label="Copy title", command=lambda: self.copy("title"))

        det = ttk.Frame(self, padding=(10, 8, 10, 0))
        det.pack(fill="x")
        ttk.Label(det, text="Format:").pack(side="left")
        self.combo = ttk.Combobox(det, state="disabled", width=28)
        self.combo.pack(side="left", padx=6)
        self.dl_btn = ttk.Button(det, text="⬇ Download", command=self.start_download, state="disabled")
        self.dl_btn.pack(side="left")
        self.cancel_btn = ttk.Button(det, text="Cancel", command=self.cancel_download, state="disabled")
        self.cancel_btn.pack(side="left", padx=(4, 0))
        self.open_btn = ttk.Button(det, text="Open page", command=self.open_page, state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        self.retry_btn = ttk.Button(det, text="↻ Retry", command=self.retry, state="disabled")
        self.retry_btn.pack(side="left", padx=(4, 0))
        self.folder_btn = ttk.Button(det, text="Show file", command=self.show_file, state="disabled")
        self.folder_btn.pack(side="left", padx=(4, 0))
        self.prog = ttk.Progressbar(det, length=140, mode="determinate")
        self.prog.pack(side="right")

        self.status = ttk.Label(self, text="Type a title, author or ISBN, choose a language and press Search.", padding=10)
        self.status.pack(fill="x")
        self.update_headings()

    # ---------------- message queue ----------------
    def poll(self):
        """Drain the message queue in slices of <= 30 ms so the window stays responsive
        (close, move, cancel) even when thousands of results arrive at once."""
        t0 = time.time()
        try:
            while time.time() - t0 < 0.03:
                try:
                    msg = self.msgs.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(msg)
                except Exception as e:  # a failure in one message must not stop the interface
                    log.exception("message %s: %r", msg[0], e)
            if self.resort_dirty and (not self.searching or time.time() - self.last_resort > 0.5):
                self.resort()  # re-sort at most every 0.5 s while results arrive
            if self.searching and time.time() - self.search_started > SEARCH_DEADLINE:
                self.search_timeout()
        finally:
            self.after(10 if not self.msgs.empty() else 100, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind in ("res", "err"):
            _, wave, name, payload = msg
            if wave != self.wave or not self.searching or name in self.search_done:
                return
            if kind == "res":
                self.add_results(payload)
            else:
                self.search_failed.append(name)
            self.search_done.add(name)
            self.update_search_status()
            if len(self.search_done) >= self.search_total:
                self.finish()
        elif kind == "resolved":
            self.after_resolve(*msg[1:])
        elif kind == "prog":
            self.set_prog(msg[1], msg[2])
        elif kind == "dl_done":
            self.dl_done(*msg[1:])

    # ---------------- sorting ----------------
    def sort_by(self, col, keep=False):
        if not keep:
            self.sort_desc = (not self.sort_desc) if col == self.sort_col else False
        elif col != self.sort_col:
            self.sort_desc = False
        self.sort_col = col
        self.resort()

    def toggle_dir(self):
        self.sort_desc = not self.sort_desc
        self.resort()

    def update_headings(self):
        for c, t in HEAD.items():
            arrow = (" ▼" if self.sort_desc else " ▲") if c == self.sort_col else ""
            self.tree.heading(c, text=t + arrow)
        inv = {v: k for k, v in SORT_LABELS.items()}
        self.sort_box.set(inv.get(self.sort_col, "Relevance"))
        if self.sort_col == "rel":
            txt = "Worst → best" if self.sort_desc else "Best → worst"
        elif self.sort_col in ("year", "fmt"):
            txt = "9 → 0" if self.sort_desc else "0 → 9"
        else:
            txt = "Z → A" if self.sort_desc else "A → Z"
        self.dir_btn.config(text=txt)

    def resort(self):
        """Re-sort the table, moving only the rows that are not already in place."""
        self.resort_dirty, self.last_resort = False, time.time()
        desired = order_items(list(self.iids.values()), self.sort_col, self.sort_desc)
        current = list(self.tree.get_children())
        for idx, it in enumerate(desired):
            if idx >= len(current) or current[idx] != it.iid:
                self.tree.move(it.iid, "", idx)
                if it.iid in current:
                    current.remove(it.iid)
                current.insert(idx, it.iid)
        self.update_headings()

    # ---------------- external sites and settings ----------------
    def rebuild_menu(self):
        self.menu.delete(0, "end")
        for name, fn, needs_q in sources.WEB_SITES:
            self.menu.add_command(label=name, command=lambda f=fn, n=needs_q: self.open_web(f, n))
        if self.cfg["sites"]:
            self.menu.add_separator()
            for site in self.cfg["sites"]:
                tpl = site["url"]
                self.menu.add_command(
                    label=site["name"],
                    command=lambda t=tpl: self.open_web(lambda q, t=t: settings.build_url(t, q), "{q}" in t))

    def open_web(self, fn, needs_query=True):
        q = self.entry.get().strip()
        if needs_query and not q:
            self.status.config(text="Type what you want to search for first.")
            return
        url = fn(q)
        if safe_url(url):
            webbrowser.open(url)

    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("700x640")
        win.transient(self)
        pad = ttk.Frame(win, padding=12)
        pad.pack(fill="both", expand=True)

        adv = ttk.LabelFrame(pad, text="Advanced options (optional)", padding=8)
        adv.pack(fill="x")
        ttk.Label(adv, text="Google Books API key:").grid(row=0, column=0, sticky="w")
        gk = ttk.Entry(adv)
        gk.insert(0, self.cfg["google_key"])
        gk.grid(row=0, column=1, padx=6, pady=2, sticky="we")
        ttk.Label(adv, text="Contact (email or website):").grid(row=1, column=0, sticky="w")
        ct = ttk.Entry(adv)
        ct.insert(0, self.cfg["contact"])
        ct.grid(row=1, column=1, padx=6, pady=2, sticky="we")
        ttk.Label(adv, text="Europeana API key:").grid(row=2, column=0, sticky="w")
        eu = ttk.Entry(adv)
        eu.insert(0, self.cfg["europeana_key"])
        eu.grid(row=2, column=1, padx=6, pady=2, sticky="we")
        ttk.Label(adv, text="CORE API key:").grid(row=3, column=0, sticky="w")
        co = ttk.Entry(adv)
        co.insert(0, self.cfg["core_key"])
        co.grid(row=3, column=1, padx=6, pady=2, sticky="we")
        adv.columnconfigure(1, weight=1)
        ttk.Label(adv, wraplength=620, justify="left", foreground="#666", text=(
            "All of them are free. Google Books: without a key you share its quota with everyone on your "
            "connection. Europeana works with a public demo key, but your own gives you a larger quota "
            "(europeana.eu/en/apis). CORE (core.ac.uk/services/api) is only enabled if you paste a key: "
            "it adds thousands of books and texts from university repositories. The contact is added to the "
            "User-Agent: Open Library and other free APIs ask for it.")
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        msg = ttk.Label(pad, text="")

        def save_adv():
            self.cfg.update(google_key=gk.get().strip(), contact=ct.get().strip(),
                            europeana_key=eu.get().strip(), core_key=co.get().strip())
            net.STATE.update({k: self.cfg[k] for k in ("google_key", "contact", "europeana_key", "core_key")})
            msg.config(text="Saved." if settings.save_settings(self.cfg) else "Could not save to disk.")
        ttk.Button(adv, text="Save", command=save_adv).grid(row=0, column=2, rowspan=4, padx=4)

        ttk.Label(pad, wraplength=640, justify="left", text=(
            "\nMy search sites: they will appear in the \u201cAlso search the web\u201d menu. In the address, "
            "{q} is replaced by your search.\nExample: https://www.example.org/search?q={q}")).pack(anchor="w")
        lb = tk.Listbox(pad, height=7)
        lb.pack(fill="both", expand=True, pady=8)

        def fill():
            lb.delete(0, "end")
            for x in self.cfg["sites"]:
                lb.insert("end", "%s  →  %s" % (x["name"], x["url"]))
        fill()
        row = ttk.Frame(pad)
        row.pack(fill="x")
        ttk.Label(row, text="Name:").grid(row=0, column=0, sticky="w")
        name = ttk.Entry(row, width=28)
        name.grid(row=0, column=1, padx=6, pady=2, sticky="we")
        ttk.Label(row, text="Address:").grid(row=1, column=0, sticky="w")
        url = ttk.Entry(row)
        url.grid(row=1, column=1, padx=6, pady=2, sticky="we")
        row.columnconfigure(1, weight=1)
        msg.pack(anchor="w", pady=(4, 0))

        def add():
            n, u = name.get().strip(), url.get().strip()
            if not n or not settings.valid_site_url(u):
                msg.config(text="Enter a name and an address starting with http:// or https://")
                return
            self.cfg["sites"].append({"name": n, "url": u})
            ok = settings.save_settings(self.cfg)
            name.delete(0, "end")
            url.delete(0, "end")
            msg.config(text="Added." if ok else "Added (could not save to disk).")
            fill()
            self.rebuild_menu()

        def remove():
            sel = lb.curselection()
            if sel:
                del self.cfg["sites"][sel[0]]
                settings.save_settings(self.cfg)
                fill()
                self.rebuild_menu()
        btns = ttk.Frame(pad)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Add", command=add).pack(side="left")
        ttk.Button(btns, text="Remove selected", command=remove).pack(side="left", padx=6)
        ttk.Button(btns, text="Close", command=win.destroy).pack(side="right")

    def about(self):
        messagebox.showinfo("About", (
            "%s %s\n\nSearches for free and legal books across %d sources, including Open Library, "
            "Project Gutenberg, Google Books, Internet Archive, Wikisource, Zenodo, OpenAlex, DOAB, OAPEN, "
            "LibriVox and Europeana.\n\n"
            "Data and log folder: %s") % (APP_NAME, __version__, len(sources.SOURCES), settings.data_dir()))

    # ---------------- search ----------------
    def cur_lang(self):
        return LANGS.get(self.lang_box.get())  # None = all languages

    def rerun(self):
        if self.entry.get().strip():
            self.start()

    def on_search_btn(self):
        if self.searching:
            self.cancel_search()
        else:
            self.start()

    def remember(self, q):
        hist = [q] + [h for h in self.cfg["history"] if h.lower() != q.lower()]
        self.cfg["history"] = hist[:20]
        self.entry["values"] = self.cfg["history"]

    def start(self):
        """New search: clear the results and start the first batch."""
        q = self.entry.get().strip()
        if not q:
            return
        self.cancel_search(silent=True)
        self.gen += 1
        self.store.clear()
        self.iids.clear()
        self.tree.delete(*self.tree.get_children())
        self.set_details(None)
        self.query, self.isbn, self.page = q, isbn_of(q), 0
        self.qwords = [] if self.isbn else query_words(q)
        self.can_widen, self.widened, self.failed_all = not self.isbn, False, []
        self.remember(q)
        self.launch(page=0, wide=False)

    def launch(self, page, wide):
        """One batch of queries to all sources (does not clear what was already found)."""
        self.wave += 1
        L = None if (self.isbn or wide) else self.cur_lang()  # ISBN and extended mode: all languages
        self.searching, self.search_started = True, time.time()
        self.search_total, self.search_done, self.search_failed = len(sources.SOURCES), set(), []
        self.btn.config(text="Cancel")
        if self.isbn:
            msg = "Searching ISBN in %d sources…"
        else:
            msg = "Extended search in %d sources…" if wide else "Searching %d sources…"
        self.status.config(text=msg % self.search_total)
        self.update_buttons()
        self.search_cancel = sources.start_search(self.query, L, self.loans.get(), self.wave, self.msgs,
                                                  page=page, wide=wide)

    def load_more(self):
        if self.searching or not self.query or self.isbn:
            return
        self.page += 1
        self.can_widen = False
        self.launch(page=self.page, wide=self.widened)

    def cancel_search(self, silent=False):
        was = self.searching
        if self.search_cancel:
            self.search_cancel.set()
            self.search_cancel = None
        self.searching = False
        self.btn.config(text="Search")
        self.update_buttons()
        if was and not silent:
            self.status.config(text="Search cancelled. %d books found so far." % len(self.iids))

    def search_timeout(self):
        for name, _ in sources.SOURCES:
            if name not in self.search_done:
                self.search_failed.append(name + " (timed out)")
        self.search_done = {n for n, _ in sources.SOURCES}
        if self.search_cancel:
            self.search_cancel.set()
        self.finish()

    def update_search_status(self):
        self.status.config(text="%d books found… (%d/%d sources)" % (
            len(self.iids), len(self.search_done), self.search_total))

    def add_results(self, res):
        mode = FILTERS[self.filter_box.get()]
        L = self.cur_lang()
        for r in res:
            if not passes(r, self.qwords, mode):
                continue
            it, is_new = self.store.merge(r)
            if is_new:
                it.iid = self.tree.insert("", "end")
                self.iids[it.iid] = it
            it.rel = relevance(it, self.qwords, L)
            self.refresh_row(it)
        self.resort_dirty = True

    def finish(self):
        self.searching, self.search_cancel = False, None
        self.btn.config(text="Search")
        if self.resort_dirty:
            self.resort()
        self.failed_all += self.search_failed
        n, total = len(self.iids), self.search_total
        all_failed = len({f.split(" (")[0] for f in self.search_failed}) >= total
        if self.can_widen and not self.isbn and n < WIDEN_BELOW and not all_failed:
            # few books: widen automatically to all languages, without stop words and with single keywords
            self.can_widen, self.widened = False, True
            self.launch(page=0, wide=True)
            self.status.config(text="Only %d books: widening the search (all languages and single keywords)…" % n)
            return
        failed = self.failed_all
        if failed and len(set(f.split(" (")[0] for f in failed)) >= total:
            text = ("Could not connect to any source. Check your internet connection "
                    "(☰ More ▸ View error log).")
        else:
            text = ("%d books found%s. Select one to download it."
                    % (n, " (extended search)" if self.widened else "") if n
                    else "No results. Try another title, author or language.")
            if failed:
                names = sorted(set(failed))
                text += " No response from %d sources: %s." % (len(names), ", ".join(names))
            if n and not self.isbn:
                text += " Press \u201c＋ More results\u201d to request more pages."
        self.status.config(text=text)
        self.update_buttons()

    def refresh_row(self, it):
        exts = []
        for f in sorted_files(it.files):
            if f["ext"].upper() not in exts:
                exts.append(f["ext"].upper())
        if exts:
            fmt = ", ".join(exts) + (" +" if it.resolvers else "")
        elif it.rstate == "failed":
            fmt = "lookup failed"
        else:
            fmt = "see on select" if it.resolvers else "web only"
        webonly = not it.files and not it.resolvers
        self.tree.item(it.iid, values=(it.title, it.author or "—", it.year, it.lang.upper() or "—", fmt,
                                       ", ".join(it.sources)),
                       tags=("webonly",) if webonly else ())

    # ---------------- details / formats ----------------
    def current(self):
        sel = self.tree.selection()
        return self.iids.get(sel[0]) if sel else None

    def on_select(self):
        it = self.current()
        self.set_details(it)
        self.maybe_resolve(it)

    def maybe_resolve(self, it, force=False):
        if not it or not it.resolvers or it.rstate == "loading":
            return
        if it.rstate == "failed" and not force:
            return
        it.rstate = "loading"
        threading.Thread(target=self.resolve_worker, args=(it, list(it.resolvers), self.gen), daemon=True).start()
        if self.current() is it:
            self.set_details(it)

    def resolve_worker(self, it, resolvers, gen):
        files, ok, failed = [], [], False
        for fn in resolvers:
            try:
                files += fn()
                ok.append(fn)
            except Exception as e:
                failed = True
                log.warning("formats of '%s': %s (%r)", it.title, net.describe_error(e), e)
        self.msgs.put(("resolved", gen, it, files, ok, failed))

    def after_resolve(self, gen, it, files, ok, failed):
        if gen != self.gen:
            return
        seen = {f["url"] for f in it.files}
        it.files += [f for f in files if f["url"] not in seen]
        it.resolvers = [r for r in it.resolvers if r not in ok]
        it.rstate = "failed" if failed else ("idle" if it.resolvers else "done")
        self.refresh_row(it)
        if self.sort_col == "fmt":
            self.resort()
        if self.current() is it:
            self.set_details(it)
            if it.rstate == "idle":
                self.maybe_resolve(it)

    def set_details(self, it):
        self.prog.config(mode="determinate")
        self.prog["value"] = 0
        self.fmt_map = {}
        if not it:
            self.combo.config(state="disabled", values=())
            self.combo.set("")
            self.update_buttons()
            return
        for f in sorted_files(it.files):
            label = "%s · %s" % (f["ext"].upper(), f["src"])
            n = 2
            while label in self.fmt_map:
                label = "%s · %s (%d)" % (f["ext"].upper(), f["src"], n)
                n += 1
            self.fmt_map[label] = f
        labels = list(self.fmt_map)
        if labels:
            self.combo.config(state="readonly", values=labels)
            self.combo.set(labels[0])
        else:
            self.combo.config(state="disabled", values=())
            if it.rstate == "failed":
                self.combo.set("Could not get the formats")
            elif it.resolvers:
                self.combo.set("Looking up formats…")
            else:
                self.combo.set("No direct download")
        self.update_buttons()

    def update_buttons(self):
        it = self.current()
        st = lambda ok: "normal" if ok else "disabled"
        self.dl_btn.config(state=st(self.fmt_map and not self.downloading))
        self.cancel_btn.config(state=st(self.downloading))
        self.open_btn.config(state=st(it))
        self.retry_btn.config(state=st(it and it.rstate == "failed"))
        self.folder_btn.config(state=st(self.last_file))
        self.more_btn.config(state=st(self.query and not self.isbn and not self.searching))

    def retry(self):
        self.maybe_resolve(self.current(), force=True)

    # ---------------- actions ----------------
    def activate(self):
        it = self.current()
        if not it:
            return
        if self.fmt_map and not self.downloading:
            self.start_download()
        else:
            self.open_page()

    def popup(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.ctx.entryconfig(0, state="normal" if self.fmt_map and not self.downloading else "disabled")
        self.ctx.tk_popup(event.x_root, event.y_root)

    def copy(self, what):
        it = self.current()
        if it:
            self.clipboard_clear()
            self.clipboard_append(it.page if what == "page" else it.title)

    def open_page(self):
        it = self.current()
        if it and safe_url(it.page):
            webbrowser.open(it.page)

    def start_download(self):
        it, f = self.current(), self.fmt_map.get(self.combo.get())
        if not it or not f or self.downloading:
            return
        opts = dict(initialfile="%s.%s" % (safe_name(it.title), f["ext"]), defaultextension="." + f["ext"],
                    filetypes=[(f["ext"].upper(), "*." + f["ext"]), ("All files", "*.*")])
        if self.cfg["last_dir"] and os.path.isdir(self.cfg["last_dir"]):
            opts["initialdir"] = self.cfg["last_dir"]
        path = filedialog.asksaveasfilename(**opts)
        if not path:
            return
        self.cfg["last_dir"] = os.path.dirname(path)
        self.downloading, self.dl_cancel = True, threading.Event()
        self.update_buttons()
        self.status.config(text="Downloading…")
        threading.Thread(target=self.dl_worker, args=(f, path, self.dl_cancel), daemon=True).start()

    def cancel_download(self):
        if self.dl_cancel:
            self.dl_cancel.set()

    def dl_worker(self, f, path, cancel):
        def prog(d, t):
            now = time.time()
            if now - self._last_prog > 0.1 or d == t:
                self._last_prog = now
                self.msgs.put(("prog", d, t))
        try:
            net.download(f["url"], path, f["ext"], prog, cancel)
            self.msgs.put(("dl_done", True, "✔ Saved to: " + path, path))
        except net.Cancelled:
            self.msgs.put(("dl_done", False, "Download cancelled.", None))
        except Exception as e:
            log.warning("download %s: %s (%r)", f["url"], net.describe_error(e), e)
            self.msgs.put(("dl_done", False, "Download failed (%s). Try another format or press "
                                             "\u201cOpen page\u201d." % net.describe_error(e), None))

    def set_prog(self, d, t):
        if t:
            self.prog.config(mode="determinate")
            self.prog["value"] = min(100, d * 100 / t)
        else:
            self.prog.config(mode="indeterminate")
            self.prog.step(3)

    def dl_done(self, ok, text, path):
        self.downloading, self.dl_cancel = False, None
        self.prog.config(mode="determinate")
        self.prog["value"] = 100 if ok else 0
        if ok:
            self.last_file = path
            settings.save_settings(self.cfg)
        self.status.config(text=text)
        self.update_buttons()

    def show_file(self):
        if self.last_file and os.path.exists(self.last_file):
            reveal_file(self.last_file)
        else:
            self.status.config(text="The file is no longer at that location.")

    def export_csv(self):
        items = [self.iids[i] for i in self.tree.get_children() if i in self.iids]
        if not items:
            self.status.config(text="There are no results to export.")
            return
        path = filedialog.asksaveasfilename(initialfile="results.csv", defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:  # ';' delimiter + BOM: opens directly in Excel on most European locales
                w = csv.writer(fh, delimiter=";")
                w.writerow(["Title", "Author", "Year", "Language", "Formats", "Sources", "Page", "Download links"])
                for it in items:
                    fs = sorted_files(it.files)
                    w.writerow([it.title, it.author, it.year, it.lang, ", ".join(sorted({f["ext"] for f in fs})),
                                ", ".join(it.sources), it.page, " | ".join("%s: %s" % (f["ext"], f["url"]) for f in fs)])
            self.status.config(text="✔ %d results exported to %s" % (len(items), path))
        except OSError as e:
            self.status.config(text="Could not save the CSV: %s" % e)

    def on_close(self):
        self.cancel_search(silent=True)
        self.cancel_download()
        self.cfg.update(lang=self.lang_box.get(), filter=self.filter_box.get(), loans=bool(self.loans.get()))
        settings.save_settings(self.cfg)
        self.destroy()

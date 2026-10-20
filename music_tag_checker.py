#!/usr/bin/env python3
"""MP3 Tag Finder - scan a folder of music files and see which tags each track is missing.

Usage:  python music_tag_checker.py [folder]
"""
from __future__ import annotations

import csv
import io
import json
import os
import platform
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

from cleanup_dialog import CleanupDialog, plan_cleanup
from edit_dialog import EDITABLE_FIELDS, SINGLE_TRACK_FIELDS, SetTagDialog
from review_dialog import ReviewDialog
from tag_finder import HAS_YTDLP
from updater import UpdateDialog, check_in_background, is_newer
from version import __version__
from tag_reader import (
    ARTWORK, DEFAULT_REQUIRED, FIELD_LABELS, FIELDS, TEXT_FIELDS,
    TrackInfo, find_audio_files, load_artwork, read_track,
)

try:  # Pillow is optional; it enables JPEG artwork previews.
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

ARTWORK_PREVIEW_SIZE = 260
SHOW_OPTIONS = ["All tracks", "Incomplete only", "Complete only", "Read errors"]
COLUMNS = ["file", "folder"] + TEXT_FIELDS + [ARTWORK, "missing"]
COLUMN_TITLES = {"file": "File", "folder": "Folder", "missing": "Missing", **FIELD_LABELS}
COLUMN_WIDTHS = {
    "file": 220, "folder": 130, "title": 170, "artist": 140, "album": 150, "albumartist": 120,
    "genre": 90, "year": 60, "track": 60, "disc": 55, "composer": 110,
    ARTWORK: 65, "missing": 200,
}
NUMERIC_FIELDS = {"year", "track", "disc"}
EMPTY = "—"


SETTINGS_PATH = os.environ.get("MUSIC_TAG_CHECKER_SETTINGS",
                               os.path.join(os.path.expanduser("~"), ".music_tag_checker.json"))


def load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError:
        pass  # remembering settings is a convenience; never fail because of it


def _leading_int(text: str) -> int:
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else 0


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _scan_worker(folder: str, recursive: bool, cancel: threading.Event, out: queue.Queue):
    try:
        paths = find_audio_files(folder, recursive)
        out.put(("total", len(paths)))
        for path in paths:
            if cancel.is_set():
                break
            out.put(("track", read_track(path)))
        out.put(("done", cancel.is_set()))
    except Exception as exc:
        out.put(("fatal", str(exc)))


class MusicTagChecker(tk.Tk):
    def __init__(self, initial_folder: str | None = None):
        super().__init__()
        self.title(f"MP3 Tag Finder {__version__}")
        self.geometry("1400x780")
        self.minsize(900, 500)

        self.tracks: list[TrackInfo] = []
        self.scan_root = ""
        self.scan_queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.scanning = False
        self.total_files = 0
        self.sort_column = "file"
        self.sort_reverse = False
        self._artwork_image = None  # keep a reference so Tk doesn't garbage-collect it

        settings = load_settings()
        last_folder = settings.get("last_folder") or ""
        if not initial_folder and last_folder and os.path.isdir(last_folder):
            initial_folder = last_folder  # reopen where we left off
        self.folder_var = tk.StringVar(value=initial_folder or last_folder)
        self.recursive_var = tk.BooleanVar(value=settings.get("include_subfolders", True))
        self.show_var = tk.StringVar(value=SHOW_OPTIONS[0])
        self.missing_filter_var = tk.StringVar(value="Any field")
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a music folder and press Scan.")
        self.video_var = tk.BooleanVar(value=settings.get("search_video_sites", True) and HAS_YTDLP)
        self._detail_buttons: list[tk.Widget] = []
        self.skipped_version = settings.get("skipped_version", "")
        required = set(settings.get("required_fields", DEFAULT_REQUIRED))
        self.required_vars = {key: tk.BooleanVar(value=key in required) for key, _ in FIELDS}

        self._set_window_icon()
        self._build_ui()
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(1500, lambda: check_in_background(self, lambda r: self.on_update_result(r, manual=False)))
        if initial_folder:
            self.after(200, self.start_scan)

    def _set_window_icon(self):
        """Title bar / taskbar / Dock icon. The packaged app bundles icon.png next to its code."""
        here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for path in (os.path.join(here, "icon.png"), os.path.join(here, "build_assets", "icon.png"),
                     os.path.join(here, "assets", "logo.png")):
            if os.path.exists(path):
                try:
                    photo = tk.PhotoImage(file=path)
                    factor = max(1, photo.width() // 256)
                    self._window_icon = photo.subsample(factor) if factor > 1 else photo
                    self.iconphoto(True, self._window_icon)
                except tk.TclError:
                    pass
                return

    # ---------------------------------------------------------------- updates
    def _build_menu(self):
        menubar = tk.Menu(self)
        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Check for Updates…", command=self.check_for_updates)
        help_menu.add_command(label="About MP3 Tag Finder", command=lambda: messagebox.showinfo(
            "About", f"MP3 Tag Finder {__version__}\n\nhttps://github.com/corduroy420/music-tag-checker"))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menubar)

    def check_for_updates(self):
        self.status_var.set("Checking for updates…")
        check_in_background(self, lambda release: self.on_update_result(release, manual=True))

    def on_update_result(self, release: dict | None, manual: bool):
        if manual:
            self.refresh_view() if self.tracks else self.status_var.set("")
        if release and is_newer(release["version"]) and (manual or release["version"] != self.skipped_version):
            UpdateDialog(self, release, on_skip=self.skip_version)
        elif manual and release:
            messagebox.showinfo("No updates", f"You have the latest version ({__version__}).")
        elif manual:
            messagebox.showwarning("Couldn't check for updates",
                                   "GitHub could not be reached, or no release has been published yet.")

    def skip_version(self, version: str):
        self.skipped_version = version
        self.save_settings()

    # ---------------------------------------------------------------- UI setup
    def _build_ui(self):
        style = ttk.Style(self)
        # Work around Tk 8.6.9 ignoring Treeview tag colours.
        for option in ("foreground", "background"):
            style.map("Treeview", **{option: [
                e for e in style.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")
            ]})
        style.configure("Treeview", rowheight=22)

        # Folder row
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Music folder:").pack(side="left")
        entry = ttk.Entry(top, textvariable=self.folder_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.start_scan())
        ttk.Button(top, text="Browse…", command=self.browse).pack(side="left")
        ttk.Checkbutton(top, text="Include subfolders", variable=self.recursive_var).pack(side="left", padx=10)
        self.scan_button = ttk.Button(top, text="Scan", command=self.toggle_scan)
        self.scan_button.pack(side="left")
        ttk.Button(top, text="Export CSV…", command=self.export_csv).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Find missing tags…", command=self.find_missing).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Remove emojis…", command=self.remove_emojis).pack(side="left", padx=(6, 0))

        # Which fields count as missing
        req = ttk.LabelFrame(self, text="Flag a track as incomplete when it has no…", padding=(8, 4))
        req.pack(fill="x", padx=10, pady=4)
        for key, label in FIELDS:
            ttk.Checkbutton(req, text=label, variable=self.required_vars[key],
                            command=self.refresh_view).pack(side="left", padx=(0, 10))

        # Filters
        filters = ttk.Frame(self, padding=(10, 2))
        filters.pack(fill="x")
        ttk.Label(filters, text="Show:").pack(side="left")
        show = ttk.Combobox(filters, textvariable=self.show_var, values=SHOW_OPTIONS, state="readonly", width=16)
        show.pack(side="left", padx=(4, 12))
        show.bind("<<ComboboxSelected>>", lambda e: self.refresh_view())
        ttk.Label(filters, text="Missing:").pack(side="left")
        missing = ttk.Combobox(filters, textvariable=self.missing_filter_var, state="readonly", width=14,
                               values=["Any field"] + [label for _, label in FIELDS])
        missing.pack(side="left", padx=(4, 12))
        missing.bind("<<ComboboxSelected>>", lambda e: self.refresh_view())
        ttk.Label(filters, text="Search:").pack(side="left")
        search = ttk.Entry(filters, textvariable=self.search_var, width=30)
        search.pack(side="left", padx=4)
        self.search_var.trace_add("write", lambda *a: self.refresh_view())
        video_label = "Also search SoundCloud/YouTube for artwork"
        ttk.Checkbutton(filters, variable=self.video_var, state="normal" if HAS_YTDLP else "disabled",
                        text=video_label if HAS_YTDLP else video_label + " (install yt-dlp)").pack(side="right")

        # Main area: track table + detail pane
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=6)

        table = ttk.Frame(paned)
        self.tree = ttk.Treeview(table, columns=COLUMNS, show="headings", selectmode="extended")
        for col in COLUMNS:
            self.tree.heading(col, text=COLUMN_TITLES[col], command=lambda c=col: self.sort_by(c))
            anchor = "center" if col in NUMERIC_FIELDS or col == ARTWORK else "w"
            self.tree.column(col, width=COLUMN_WIDTHS[col], minwidth=40, anchor=anchor, stretch=False)
        self.tree.column("missing", stretch=True)
        yscroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.tag_configure("incomplete", background="#fde4e2", foreground="#3b0d0a")
        self.tree.tag_configure("complete", background="#e2f4df", foreground="#0f2e0b")
        self.tree.tag_configure("error", background="#e4e4e4", foreground="#555555")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.reveal_selected())
        for shortcut in ("<Command-a>", "<Control-a>"):
            self.tree.bind(shortcut, lambda e: self.tree.selection_set(self.tree.get_children()))
        # Right-click menu (Button-2 / Control-click on macOS, Button-3 elsewhere)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Select all in this folder", command=self.select_folder)
        self.menu.add_command(label="Select all in this folder and its subfolders",
                              command=lambda: self.select_folder(include_subfolders=True))
        self.menu.add_separator()
        self.menu.add_command(label="Set genre on selected…", command=lambda: self.set_tag("genre"))
        self.menu.add_command(label="Set another tag on selected…", command=self.set_tag)
        self.menu.add_command(label="Find missing tags for selected", command=self.find_missing)
        self.menu.add_command(label="Remove emojis from selected…", command=self.remove_emojis)
        self.menu.add_separator()
        self.menu.add_command(label="Show in file manager", command=self.reveal_selected)
        for button in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.tree.bind(button, self.open_menu)
        paned.add(table, weight=4)

        detail = ttk.Frame(paned, padding=(10, 0, 0, 0))
        self.art_label = ttk.Label(detail, text="No track selected", anchor="center", justify="center")
        self.art_label.pack(fill="x", pady=(0, 8))
        self.detail_text = tk.Text(detail, width=40, wrap="word", relief="flat", padx=6, pady=6,
                                   highlightthickness=0, borderwidth=0)
        self.detail_text.pack(fill="both", expand=True)
        self.detail_text.tag_configure("heading", font=("TkDefaultFont", 12, "bold"))
        self.detail_text.tag_configure("label", font=("TkDefaultFont", 11, "bold"))
        self.detail_text.tag_configure("missing", foreground="#c0392b")
        self.detail_text.tag_configure("ok", foreground="#2e8b3e")
        self.detail_text.configure(state="disabled")
        self.find_button = ttk.Button(detail, text="Find missing tags", command=self.find_missing)
        self.find_button.pack(fill="x", pady=(6, 0))
        self.set_button = ttk.Button(detail, text="Set a tag on selected…", command=self.set_tag)
        self.set_button.pack(fill="x", pady=(4, 0))
        self.folder_button = ttk.Button(detail, text="Select all in this folder", command=self.select_folder)
        self.folder_button.pack(fill="x", pady=(4, 0))
        ttk.Button(detail, text="Show in file manager", command=self.reveal_selected).pack(fill="x", pady=(4, 0))
        paned.add(detail, weight=1)

        # Status bar
        bottom = ttk.Frame(self, padding=(10, 0, 10, 8))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=200)
        self.progress.pack(side="right")
        ttk.Label(bottom, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

    # ---------------------------------------------------------------- scanning
    def browse(self):
        folder = filedialog.askdirectory(title="Choose a music folder",
                                         initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if folder:
            self.folder_var.set(folder)
            self.start_scan()

    def toggle_scan(self):
        if self.scanning:
            self.cancel_event.set()
        else:
            self.start_scan()

    def start_scan(self):
        folder = os.path.expanduser(self.folder_var.get().strip())
        if not folder:
            self.browse()
            return
        if not os.path.isdir(folder):
            messagebox.showerror("Folder not found", f"This folder does not exist:\n{folder}")
            return
        if self.scanning:
            return
        self.tracks = []
        self.scan_root = folder
        self.save_settings()
        self.tree.delete(*self.tree.get_children())
        self.clear_details()
        self.scanning = True
        self.cancel_event = threading.Event()
        self.scan_queue = queue.Queue()
        self.scan_button.configure(text="Cancel")
        self.progress.configure(value=0, maximum=1)
        self.status_var.set("Looking for music files…")
        threading.Thread(target=_scan_worker, daemon=True,
                         args=(folder, self.recursive_var.get(), self.cancel_event, self.scan_queue)).start()
        self.after(50, self.poll_scan)

    def poll_scan(self):
        required = self.required()
        try:
            for _ in range(500):
                kind, payload = self.scan_queue.get_nowait()
                if kind == "total":
                    self.total_files = payload
                    self.progress.configure(maximum=max(payload, 1))
                elif kind == "track":
                    self.tracks.append(payload)
                    if self.matches_filter(payload, required):
                        self.insert_row(len(self.tracks) - 1, payload, required)
                elif kind == "done":
                    self.finish_scan(cancelled=payload)
                    return
                elif kind == "fatal":
                    self.finish_scan(cancelled=True)
                    messagebox.showerror("Scan failed", payload)
                    return
        except queue.Empty:
            pass
        self.progress.configure(value=len(self.tracks))
        self.status_var.set(f"Scanning… {len(self.tracks)} of {self.total_files} files")
        self.after(50, self.poll_scan)

    def finish_scan(self, cancelled: bool):
        self.scanning = False
        self.scan_button.configure(text="Scan")
        self.progress.configure(value=len(self.tracks))
        self.refresh_view()
        if not self.tracks:
            self.status_var.set("No music files found in that folder." if not cancelled else "Scan cancelled.")
        elif cancelled:
            self.status_var.set("Scan cancelled — " + self.status_var.get())

    def save_settings(self):
        save_settings({
            "last_folder": self.scan_root or os.path.expanduser(self.folder_var.get().strip()),
            "include_subfolders": self.recursive_var.get(),
            "required_fields": sorted(self.required()),
            "search_video_sites": self.video_var.get(),
            "skipped_version": self.skipped_version,
        })

    def on_close(self):
        self.save_settings()
        self.destroy()

    # ---------------------------------------------------------------- table
    def required(self) -> set[str]:
        return {key for key, var in self.required_vars.items() if var.get()}

    def matches_filter(self, track: TrackInfo, required: set[str]) -> bool:
        show = self.show_var.get()
        missing = track.missing(required)
        if show == "Read errors" and not track.error:
            return False
        if show == "Incomplete only" and (track.error or not missing):
            return False
        if show == "Complete only" and (track.error or missing):
            return False

        wanted = self.missing_filter_var.get()
        if wanted != "Any field":
            key = next(k for k, label in FIELDS if label == wanted)
            if track.error or key not in track.missing({key}):
                return False

        needle = self.search_var.get().strip().lower()
        if needle:
            haystack = " ".join([track.path, *track.tags.values()]).lower()
            if needle not in haystack:
                return False
        return True

    def folder_label(self, track: TrackInfo) -> str:
        """The track's folder relative to the scanned folder ('.' for the top level)."""
        return os.path.relpath(os.path.dirname(track.path), self.scan_root)

    def insert_row(self, index: int, track: TrackInfo, required: set[str]):
        if track.error:
            values = ([track.filename, self.folder_label(track)] + [EMPTY] * len(TEXT_FIELDS)
                      + [EMPTY, f"⚠ {track.error}"])
            tag = "error"
        else:
            missing = track.missing(required)
            values = [track.filename, self.folder_label(track)] + [track.tags.get(k, EMPTY) for k in TEXT_FIELDS]
            values.append("✓" if track.has_artwork else "✗")
            values.append(", ".join(FIELD_LABELS[k] for k in missing) if missing else "✓ Complete")
            tag = "incomplete" if missing else "complete"
        self.tree.insert("", "end", iid=str(index), values=values, tags=(tag,))

    def sort_key(self, column: str):
        required = self.required()

        def key(item):
            _, track = item
            if column == "file":
                return (0, track.filename.lower())
            if column == "folder":
                return (0, self.folder_label(track).lower(), track.filename.lower())
            if column == ARTWORK:
                return (0, track.has_artwork)
            if column == "missing":
                return (0, -1 if track.error else len(track.missing(required)))
            value = track.tags.get(column)
            if value is None:  # empty values always sort last
                return (1, 0 if column in NUMERIC_FIELDS else "")
            return (0, _leading_int(value) if column in NUMERIC_FIELDS else value.lower())
        return key

    def sort_by(self, column: str):
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column, self.sort_reverse = column, False
        self.refresh_view()

    def refresh_view(self):
        if self.scanning:  # rows are being added live; a full refresh happens when the scan ends
            return
        selected = self.tree.selection()
        required = self.required()
        self.tree.delete(*self.tree.get_children())

        rows = [(i, t) for i, t in enumerate(self.tracks) if self.matches_filter(t, required)]
        rows.sort(key=self.sort_key(self.sort_column), reverse=self.sort_reverse)
        for index, track in rows:
            self.insert_row(index, track, required)

        for col in COLUMNS:
            arrow = (" ▼" if self.sort_reverse else " ▲") if col == self.sort_column else ""
            self.tree.heading(col, text=COLUMN_TITLES[col] + arrow)
        still_shown = [iid for iid in selected if self.tree.exists(iid)]
        if still_shown:
            self.tree.selection_set(still_shown)
            self.tree.see(still_shown[0])
        self.update_summary(len(rows))

    def update_summary(self, shown: int):
        if not self.tracks:
            return
        required = self.required()
        errors = sum(1 for t in self.tracks if t.error)
        counts = Counter()
        incomplete = 0
        for track in self.tracks:
            if track.error:
                continue
            missing = track.missing(required)
            counts.update(missing)
            incomplete += bool(missing)
        complete = len(self.tracks) - errors - incomplete
        parts = [f"{len(self.tracks)} tracks", f"{complete} complete", f"{incomplete} incomplete"]
        if errors:
            parts.append(f"{errors} unreadable")
        text = " · ".join(parts)
        if counts:
            text += "    |    Missing: " + ", ".join(f"{FIELD_LABELS[k]} {n}" for k, n in counts.most_common())
        if shown != len(self.tracks):
            text += f"    |    showing {shown}"
        self.status_var.set(text)

    # ---------------------------------------------------------------- details
    def selected_indices(self) -> list[int]:
        return [int(iid) for iid in self.tree.selection()]

    def clear_details(self):
        self._artwork_image = None
        self.art_label.configure(image="", text="No track selected")
        self._reset_detail_text()
        self.detail_text.configure(state="disabled")
        self.find_button.configure(text="Find missing tags (all shown tracks)")

    def _reset_detail_text(self):
        for widget in self._detail_buttons:
            widget.destroy()
        self._detail_buttons = []
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")

    def _add_detail_button(self, text: str, command):
        button = ttk.Button(self.detail_text, text=text, width=5, command=command)
        self._detail_buttons.append(button)
        self.detail_text.window_create("end", window=button, padx=2)

    def _add_field_buttons(self, field: str, missing_somewhere: bool, single: bool = False):
        if missing_somewhere:
            self._add_detail_button("Find", lambda: self.find_missing(fields=[field]))
        if field in EDITABLE_FIELDS or (single and field in SINGLE_TRACK_FIELDS):
            self._add_detail_button("Set", lambda: self.set_tag(field))

    def show_details(self):
        tracks = [self.tracks[i] for i in self.selected_indices()]
        if not tracks:
            self.clear_details()
            return
        self._reset_detail_text()
        text = self.detail_text
        required = self.required()
        if len(tracks) == 1:
            self._show_single_details(tracks[0], required)
            self.find_button.configure(text="Find all missing tags")
        else:
            self._artwork_image = None
            self.art_label.configure(image="", text=f"\n\n{len(tracks)} tracks selected\n\n")
            readable = [t for t in tracks if not t.error]
            text.insert("end", f"{len(tracks)} tracks selected\n\n", "heading")
            for key, label in FIELDS:
                lacking = sum(1 for t in readable if key in t.missing({key}))
                text.insert("end", f"{label}: ", "label")
                if lacking:
                    where = "all" if lacking == len(readable) else str(lacking)
                    text.insert("end", f"missing in {where}", "missing" if key in required else ())
                elif key == ARTWORK:
                    text.insert("end", "all have artwork", "ok")
                else:
                    distinct = {t.tags[key] for t in readable}
                    text.insert("end", next(iter(distinct)) if len(distinct) == 1
                                else f"{len(distinct)} different values")
                self._add_field_buttons(key, bool(lacking))
                text.insert("end", "\n")
            if len(readable) < len(tracks):
                text.insert("end", f"\n{len(tracks) - len(readable)} unreadable files are skipped.\n", "missing")
            self.find_button.configure(text=f"Find all missing tags ({len(readable)} tracks)")
        text.configure(state="disabled")

    def _show_single_details(self, track: TrackInfo, required: set[str]):
        self.show_artwork(track)
        text = self.detail_text
        text.insert("end", track.filename + "\n", "heading")
        text.insert("end", os.path.dirname(track.path) + "\n\n")
        if track.error:
            text.insert("end", track.error + "\n", "missing")
            return
        for key, label in FIELDS:
            text.insert("end", f"{label}: ", "label")
            value = (track.artwork_info if track.has_artwork else None) if key == ARTWORK else track.tags.get(key)
            if value:
                text.insert("end", value)
            else:
                text.insert("end", "missing" if key in required else "(empty)",
                            "missing" if key in required else ())
            self._add_field_buttons(key, not value, single=True)
            text.insert("end", "\n")
        missing = track.missing(required)
        text.insert("end", "\n")
        if missing:
            text.insert("end", "Missing: " + ", ".join(FIELD_LABELS[k] for k in missing) + "\n", "missing")
        else:
            text.insert("end", "✓ All required tags present\n", "ok")
        text.insert("end", "\nFile info\n", "label")
        bitrate = f"{track.bitrate // 1000} kbps" if track.bitrate else "?"
        text.insert("end", f"{track.format} · {_fmt_duration(track.duration)} · {bitrate} · "
                           f"{track.size / 1_048_576:.1f} MB\n")

    def show_artwork(self, track: TrackInfo):
        self._artwork_image = None
        if not track.has_artwork:
            self.art_label.configure(image="", text="\n\nNo artwork\n\n")
            return
        art = load_artwork(track.path)
        if art is None:
            self.art_label.configure(image="", text="Artwork could not be loaded")
            return
        data, mime = art
        try:
            if Image is not None:
                img = Image.open(io.BytesIO(data))
                img.thumbnail((ARTWORK_PREVIEW_SIZE, ARTWORK_PREVIEW_SIZE))
                self._artwork_image = ImageTk.PhotoImage(img)
            elif mime in ("image/png", "image/gif"):
                photo = tk.PhotoImage(data=data)
                factor = max(1, -(-max(photo.width(), photo.height()) // ARTWORK_PREVIEW_SIZE))
                self._artwork_image = photo.subsample(factor)
            else:
                self.art_label.configure(image="", text=f"Has artwork ({track.artwork_info})\n"
                                                        "Install Pillow to preview JPEG artwork")
                return
            self.art_label.configure(image=self._artwork_image, text="")
        except Exception:
            self.art_label.configure(image="", text=f"Has artwork ({track.artwork_info})\nbut it could not be decoded")

    def reveal_selected(self):
        indices = self.selected_indices()
        if not indices:
            return
        track = self.tracks[indices[0]]
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.run(["open", "-R", track.path], check=False)
            elif system == "Windows":
                subprocess.run(["explorer", "/select,", os.path.normpath(track.path)], check=False)
            else:
                subprocess.run(["xdg-open", os.path.dirname(track.path)], check=False)
        except OSError as exc:
            messagebox.showerror("Could not open file manager", str(exc))

    # ---------------------------------------------------------------- folders & manual editing
    def open_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        if self.tree.selection():
            self.menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def select_folder(self, include_subfolders: bool = False):
        """Select every shown track in the same folder as the (first) selected track."""
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("Select a track first", "Click any track in the folder you want, then try again.")
            return
        folder = os.path.dirname(self.tracks[indices[0]].path)
        prefix = folder + os.sep
        rows = [iid for iid in self.tree.get_children()
                if os.path.dirname(self.tracks[int(iid)].path) == folder
                or (include_subfolders and self.tracks[int(iid)].path.startswith(prefix))]
        self.tree.selection_set(rows)
        self.tree.see(rows[0])
        self.tree.focus(rows[0])

    def set_tag(self, field: str = "genre"):
        indices = self.selected_indices()
        targets = [(i, self.tracks[i]) for i in indices if not self.tracks[i].error]
        if not targets:
            messagebox.showinfo("Select tracks first",
                                "Select the tracks to change (tip: right-click a track → "
                                "“Select all in this folder”), then try again.")
            return
        SetTagDialog(self, targets, self.tracks, self.on_tags_written, field=field)

    def remove_emojis(self):
        """Strip emojis from the tags and file names of the selected tracks (or every shown track)."""
        if self.scanning:
            return
        indices = self.selected_indices() or [int(iid) for iid in self.tree.get_children()]
        changes = plan_cleanup([(i, self.tracks[i]) for i in indices])
        if not changes:
            scope = "the selected tracks" if self.tree.selection() else "the shown tracks"
            messagebox.showinfo("No emojis found", f"None of {scope} have emojis in their tags or file names.")
            return
        CleanupDialog(self, changes, self.on_tags_written)

    # ---------------------------------------------------------------- find & fix
    def find_missing(self, fields: list[str] | None = None):
        """Look up missing tags for the selected tracks (or every shown track if none are selected).
        `fields` limits the lookup; by default it covers every field that counts as missing."""
        if self.scanning:
            return
        indices = self.selected_indices() or [int(iid) for iid in self.tree.get_children()]
        required = self.required()
        jobs = []
        for index in indices:
            track = self.tracks[index]
            if track.error:
                continue
            wanted = track.missing(set(fields) if fields else required)
            if wanted:
                jobs.append((index, track, wanted))
        if not jobs:
            messagebox.showinfo("Nothing to find", "None of these tracks are missing those tags.")
            return
        if len(jobs) > 40 and not messagebox.askyesno(
                "Find missing tags", f"Look up missing tags for {len(jobs)} tracks? This can take a few minutes "
                                     "because the music services limit how fast they can be searched."):
            return
        ReviewDialog(self, jobs, self.video_var.get(), self.on_tags_written)

    def on_tags_written(self, indices: list[int]):
        for index in indices:
            self.tracks[index] = read_track(self.tracks[index].path)
        self.refresh_view()
        self.show_details()

    # ---------------------------------------------------------------- export
    def export_csv(self):
        if not self.tracks:
            messagebox.showinfo("Nothing to export", "Scan a folder first.")
            return
        path = filedialog.asksaveasfilename(title="Export tag report", defaultextension=".csv",
                                            initialfile="tag_report.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        required = self.required()
        rows = [self.tracks[int(iid)] for iid in self.tree.get_children()]  # current view, current order
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Path", "Format", "Duration"] + [FIELD_LABELS[k] for k in TEXT_FIELDS]
                            + ["Artwork", "Missing", "Error"])
            for t in rows:
                missing = "" if t.error else ", ".join(FIELD_LABELS[k] for k in t.missing(required))
                writer.writerow([t.path, t.format, _fmt_duration(t.duration)]
                                + [t.tags.get(k, "") for k in TEXT_FIELDS]
                                + [t.artwork_info if t.has_artwork else "", missing, t.error or ""])
        self.status_var.set(f"Exported {len(rows)} tracks to {path}")


def self_test(report_path: str):
    """Used by the release build to prove the packaged app has everything it needs."""
    results = {"version": __version__}
    try:
        import PIL  # noqa: F401
        import mutagen  # noqa: F401
        import tag_finder
        results["yt_dlp"] = tag_finder.HAS_YTDLP
        results["parse"] = tag_finder.parse_filename("01 - Artist - Song (Official Video) - ArtistVEVO.mp3").title
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        results["tk"] = True
        results["ok"] = results["yt_dlp"] and results["parse"] == "Song"
    except Exception as exc:
        results["ok"] = False
        results["error"] = repr(exc)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh)
    sys.exit(0 if results["ok"] else 1)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--self-test":
        self_test(sys.argv[2])
    folder = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-psn") else None
    MusicTagChecker(folder).mainloop()


if __name__ == "__main__":
    main()

"""Window that looks up missing tags and lets the user review them before anything is written."""
from __future__ import annotations

import io
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from tag_finder import AUTO_ACCEPT, Suggestion, find_for_track, prepare_artwork
from tag_reader import ARTWORK, FIELD_LABELS, TrackInfo
from tag_writer import BACKUP_DIR_NAME, write_tags

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

CHECKED, UNCHECKED, PARTIAL = "☑", "☐", "⊟"
PREVIEW_SIZE = 240


class ReviewDialog(tk.Toplevel):
    def __init__(self, master, jobs: list[tuple[int, TrackInfo, list[str]]], use_video_sites: bool, on_written):
        """jobs: (track index, track, field keys to look up). on_written(indices) is called after writing."""
        super().__init__(master)
        self.title("Find missing tags — review before writing")
        self.geometry("1150x640")
        self.minsize(800, 400)
        self.transient(master)

        self.jobs = jobs
        self.use_video_sites = use_video_sites
        self.on_written = on_written
        self.rows: dict[str, dict] = {}       # field row iid -> {"index", "track", "suggestion", "checked"}
        self.track_rows: dict[str, dict] = {}  # track row iid -> {"track", "log"}
        self.cancel = threading.Event()
        self.results: queue.Queue = queue.Queue()
        self.finished = False
        self._preview_image = None
        self.backup_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Starting lookup…")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close)
        threading.Thread(target=self._worker, daemon=True).start()
        self.after(100, self._poll)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        header = ttk.Frame(self, padding=(10, 10, 10, 4))
        header.pack(fill="x")
        ttk.Label(header, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(header, text="Stop lookup", command=self.cancel.set)
        self.stop_button.pack(side="right")
        self.progress = ttk.Progressbar(header, length=220, maximum=max(len(self.jobs), 1))
        self.progress.pack(side="right", padx=8)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=4)

        table = ttk.Frame(paned)
        columns = ("use", "suggested", "source", "confidence")
        self.tree = ttk.Treeview(table, columns=columns, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Track / field")
        self.tree.heading("use", text="Use")
        self.tree.heading("suggested", text="Suggested value")
        self.tree.heading("source", text="Source")
        self.tree.heading("confidence", text="Match")
        self.tree.column("#0", width=280)
        self.tree.column("use", width=45, anchor="center", stretch=False)
        self.tree.column("suggested", width=330)
        self.tree.column("source", width=110, stretch=False)
        self.tree.column("confidence", width=65, anchor="center", stretch=False)
        self.tree.tag_configure("track", font=("TkDefaultFont", 12, "bold"))
        self.tree.tag_configure("notfound", foreground="#888888")
        self.tree.tag_configure("low", foreground="#b35c00")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<space>", lambda e: self._toggle_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_preview())
        paned.add(table, weight=3)

        side = ttk.Frame(paned, padding=(10, 0, 0, 0))
        self.preview_label = ttk.Label(side, anchor="center", justify="center", text="Select a row to see details")
        self.preview_label.pack(fill="x", pady=(0, 8))
        self.info = tk.Text(side, width=36, wrap="word", relief="flat", highlightthickness=0, borderwidth=0,
                            padx=6, pady=6)
        self.info.pack(fill="both", expand=True)
        self.info.tag_configure("bold", font=("TkDefaultFont", 11, "bold"))
        self.info.configure(state="disabled")
        paned.add(side, weight=1)

        footer = ttk.Frame(self, padding=(10, 4, 10, 10))
        footer.pack(fill="x")
        ttk.Label(footer, foreground="#777777",
                  text="Click a box to tick/untick (space for selected rows). "
                       "Double-click a value to edit it, or artwork to pick an image file.").pack(anchor="w")
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Tick all found", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(buttons, text="Untick all", command=lambda: self._set_all(False)).pack(side="left", padx=6)
        ttk.Checkbutton(buttons, text=f"Back up original files (to a hidden {BACKUP_DIR_NAME} folder)",
                        variable=self.backup_var).pack(side="left", padx=12)
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.write_button = ttk.Button(buttons, text="Write 0 changes", command=self.write, state="disabled")
        self.write_button.pack(side="right", padx=6)

    # ---------------------------------------------------------------- lookup
    def _worker(self):
        for n, (index, track, fields) in enumerate(self.jobs):
            if self.cancel.is_set():
                break
            self.results.put(("progress", n, track))
            log: list[str] = []
            try:
                suggestions = find_for_track(track, fields, self.use_video_sites, log.append)
            except Exception as exc:
                suggestions = [Suggestion(f, match=f"Lookup failed: {exc}") for f in fields]
            self.results.put(("result", index, track, suggestions, log))
        self.results.put(("done",))

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                message = self.results.get_nowait()
                if message[0] == "progress":
                    _, n, track = message
                    self.status_var.set(f"Looking up {n + 1} of {len(self.jobs)}: {track.filename}")
                    self.progress.configure(value=n)
                elif message[0] == "result":
                    self._add_track(*message[1:])
                else:
                    self._lookup_finished()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _lookup_finished(self):
        self.finished = True
        self.progress.configure(value=len(self.jobs))
        self.stop_button.configure(state="disabled")
        found = sum(1 for r in self.rows.values() if r["suggestion"].found)
        stopped = " (stopped early)" if self.cancel.is_set() else ""
        self.status_var.set(f"Lookup finished{stopped}: found {found} of {len(self.rows)} missing values "
                            f"for {len(self.track_rows)} tracks. Review, then write.")
        self._update_write_button()

    def _add_track(self, index: int, track: TrackInfo, suggestions: list[Suggestion], log: list[str]):
        parent = self.tree.insert("", "end", text=track.filename, open=True, tags=("track",),
                                  values=(UNCHECKED, "", "", ""))
        self.track_rows[parent] = {"track": track, "log": log}
        for s in suggestions:
            iid = self.tree.insert(parent, "end", text=FIELD_LABELS[s.field])
            self.rows[iid] = {"index": index, "track": track, "suggestion": s,
                              "checked": s.found and s.confidence >= AUTO_ACCEPT}
            self._render_row(iid)
        self._render_parent(parent)
        self._update_write_button()

    # ---------------------------------------------------------------- rows
    def _render_row(self, iid: str):
        row = self.rows[iid]
        s: Suggestion = row["suggestion"]
        if not s.found:
            values = ("", "(not found)", "", "")
            tags = ("notfound",)
        else:
            shown = f"🖼 {s.image_info or 'image'}" if s.field == ARTWORK else s.value
            values = (CHECKED if row["checked"] else UNCHECKED, shown, s.source, f"{s.confidence:.0%}")
            tags = ("low",) if s.confidence < AUTO_ACCEPT else ()
        self.tree.item(iid, values=values, tags=tags)

    def _render_parent(self, parent: str):
        states = [self.rows[c]["checked"] for c in self.tree.get_children(parent)
                  if self.rows[c]["suggestion"].found]
        box = CHECKED if states and all(states) else PARTIAL if any(states) else UNCHECKED
        found = len(states)
        total = len(self.tree.get_children(parent))
        self.tree.item(parent, values=(box, f"found {found} of {total}", "", ""))

    def _toggle(self, iid: str, state: bool | None = None):
        if iid in self.track_rows:
            children = [c for c in self.tree.get_children(iid) if self.rows[c]["suggestion"].found]
            if state is None:
                state = not all(self.rows[c]["checked"] for c in children)
            for child in children:
                self.rows[child]["checked"] = state
                self._render_row(child)
            self._render_parent(iid)
        elif iid in self.rows and self.rows[iid]["suggestion"].found:
            row = self.rows[iid]
            row["checked"] = (not row["checked"]) if state is None else state
            self._render_row(iid)
            self._render_parent(self.tree.parent(iid))
        self._update_write_button()

    def _toggle_selected(self):
        for iid in self.tree.selection():
            self._toggle(iid)
        return "break"

    def _set_all(self, state: bool):
        for parent in self.track_rows:
            self._toggle(parent, state)

    def _on_click(self, event):
        if self.tree.identify_column(event.x) == "#1" and self.tree.identify_region(event.x, event.y) == "cell":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle(iid)
                self.tree.selection_set(iid)
                return "break"
        return None

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid not in self.rows or self.tree.identify_column(event.x) == "#1":
            return
        row = self.rows[iid]
        s: Suggestion = row["suggestion"]
        if s.field == ARTWORK:
            path = filedialog.askopenfilename(parent=self, title="Choose cover image",
                                              filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*")])
            if not path:
                return
            try:
                with open(path, "rb") as fh:
                    s.image, s.image_mime, s.image_info = prepare_artwork(fh.read())
            except Exception as exc:
                messagebox.showerror("Could not use image", str(exc), parent=self)
                return
            s.match = f"Chosen file: {os.path.basename(path)}"
        else:
            value = simpledialog.askstring(f"Edit {FIELD_LABELS[s.field]}",
                                           f"{FIELD_LABELS[s.field]} for {row['track'].filename}:",
                                           initialvalue=s.value or "", parent=self)
            if value is None or not value.strip():
                return
            s.value = value.strip()
            s.match = "Edited by you"
        s.source, s.confidence = "You", 1.0
        row["checked"] = True
        self._render_row(iid)
        self._render_parent(self.tree.parent(iid))
        self._update_write_button()
        self._show_preview()
        return "break"

    def _update_write_button(self):
        count = sum(1 for r in self.rows.values() if r["checked"] and r["suggestion"].found)
        files = len({r["index"] for r in self.rows.values() if r["checked"] and r["suggestion"].found})
        label = f"Write {count} change{'s' if count != 1 else ''}" + (f" to {files} file{'s' if files != 1 else ''}" if count else "")
        self.write_button.configure(text=label, state="normal" if self.finished and count else "disabled")

    # ---------------------------------------------------------------- preview
    def _show_preview(self):
        selection = self.tree.selection()
        self._preview_image = None
        self.preview_label.configure(image="", text="")
        lines: list[tuple[str, str]] = []
        if selection and selection[0] in self.rows:
            row = self.rows[selection[0]]
            s: Suggestion = row["suggestion"]
            lines.append((FIELD_LABELS[s.field] + "\n", "bold"))
            if s.field == ARTWORK and s.image:
                self._set_preview_image(s.image)
                lines.append((f"{s.image_info}\n", ""))
            elif s.found:
                lines.append((f"{s.value}\n", ""))
            if s.found:
                lines.append((f"\nSource: {s.source} ({s.confidence:.0%} match)\n", ""))
            lines.append((f"\n{s.match}\n" if s.match else "", ""))
            if not s.found:
                lines.append(("\nDouble-click the row to enter a value yourself.\n", ""))
        elif selection and selection[0] in self.track_rows:
            info = self.track_rows[selection[0]]
            lines.append((info["track"].filename + "\n", "bold"))
            lines.append((os.path.dirname(info["track"].path) + "\n", ""))
            if info["log"]:
                lines.append(("\nProblems during lookup:\n", "bold"))
                lines.extend((f"• {msg}\n", "") for msg in info["log"])
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        for text, tag in lines:
            self.info.insert("end", text, tag)
        self.info.configure(state="disabled")

    def _set_preview_image(self, data: bytes):
        try:
            if Image is not None:
                img = Image.open(io.BytesIO(data))
                img.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))
                self._preview_image = ImageTk.PhotoImage(img)
            else:
                self._preview_image = tk.PhotoImage(data=data)
            self.preview_label.configure(image=self._preview_image)
        except Exception:
            self.preview_label.configure(text="(preview unavailable)")

    # ---------------------------------------------------------------- writing
    def write(self):
        plan: dict[int, dict] = {}
        for row in self.rows.values():
            s: Suggestion = row["suggestion"]
            if not (row["checked"] and s.found):
                continue
            entry = plan.setdefault(row["index"], {"track": row["track"], "values": {}, "artwork": None})
            if s.field == ARTWORK:
                entry["artwork"] = (s.image, s.image_mime or "image/jpeg")
            else:
                entry["values"][s.field] = s.value
        if not plan:
            return
        changes = sum(len(e["values"]) + bool(e["artwork"]) for e in plan.values())
        note = (f"\n\nOriginals will be copied to a hidden {BACKUP_DIR_NAME} folder next to each file first."
                if self.backup_var.get() else "\n\nNo backups will be made.")
        if not messagebox.askyesno("Write tags", f"Write {changes} changes to {len(plan)} files?{note}", parent=self):
            return

        self.write_button.configure(state="disabled")
        self.progress.configure(maximum=len(plan), value=0)
        errors = []
        for n, entry in enumerate(plan.values(), 1):
            self.status_var.set(f"Writing {n} of {len(plan)}: {entry['track'].filename}")
            self.update_idletasks()
            try:
                write_tags(entry["track"].path, entry["values"], entry["artwork"], backup=self.backup_var.get())
            except Exception as exc:
                errors.append(f"{entry['track'].filename}: {exc}")
            self.progress.configure(value=n)
        self.on_written(list(plan))
        if errors:
            messagebox.showwarning("Some files could not be written",
                                   f"{len(plan) - len(errors)} of {len(plan)} files updated.\n\n" + "\n".join(errors[:15]),
                                   parent=self)
        self.destroy()

    def close(self):
        self.cancel.set()
        self.destroy()

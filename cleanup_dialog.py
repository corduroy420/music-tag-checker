"""Remove emojis from tags and file names, with a before/after preview."""
from __future__ import annotations

import os
import re
import tkinter as tk
from tkinter import messagebox, ttk

from tag_reader import FIELD_LABELS, TEXT_FIELDS, TrackInfo
from tag_writer import BACKUP_DIR_NAME, write_tags

CHECKED, UNCHECKED = "☑", "☐"
FILE_NAME = "filename"

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoticons, pictographs, transport, flags, supplemental symbols…
    "\U0001FC00-\U0001FFFF"
    "\U000E0020-\U000E007F"   # tag characters (subdivision flags)
    "☀-➿"           # misc symbols and dingbats (☀ ★ ♥ ✔ ❤ …)
    "⬀-⯿"           # ⬆ ⭐ ⭕ …
    "⌚⌛⌨⏏⏩-⏺"  # ⌚ ⌛ ⏩ ⏰ …
    "〰〽㊗㊙"
    "︎️‍⃣"  # variation selectors, zero-width joiner, keycap
    "]+"
)


def remove_emojis(text: str) -> str:
    """Strip emojis and tidy what's left: empty brackets, doubled spaces, dangling separators."""
    if not has_emoji(text):
        return text
    cleaned = _EMOJI_RE.sub(" ", text)
    cleaned = re.sub(r"[(\[{]\s*[)\]}]", " ", cleaned)        # "Song (🔥)" -> "Song"
    cleaned = re.sub(r"\s+([,.!?)\]}])", r"\1", cleaned)        # no space before punctuation
    cleaned = re.sub(r"([(\[{])\s+", r"\1", cleaned)
    cleaned = re.sub(r"(\s*[-–—|~]\s*){2,}", " - ", cleaned)    # "A - 🔥 - B" -> "A - B"
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -–—|~_.,")


def has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text))


def _unique_path(path: str) -> str:
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = f"{stem} ({n}){ext}"
        n += 1
    return path


def plan_cleanup(targets: list[tuple[int, TrackInfo]]) -> list[dict]:
    """One entry per change: {"index", "track", "field", "before", "after"}. Values that would
    become empty (e.g. a title that is only emojis) are skipped."""
    changes = []
    for index, track in targets:
        if track.error:
            continue
        for field in TEXT_FIELDS:
            before = track.tags.get(field)
            if before and has_emoji(before):
                after = remove_emojis(before)
                if after and after != before:
                    changes.append({"index": index, "track": track, "field": field, "before": before, "after": after})
        stem, ext = os.path.splitext(track.filename)
        if has_emoji(stem):
            after = remove_emojis(stem)
            if after:
                changes.append({"index": index, "track": track, "field": FILE_NAME,
                                "before": track.filename, "after": after + ext})
    return changes


class CleanupDialog(tk.Toplevel):
    def __init__(self, master, changes: list[dict], on_done):
        """on_done(indices) is called after writing; renamed tracks already have their new path."""
        super().__init__(master)
        self.title("Remove emojis — review before writing")
        self.geometry("1000x560")
        self.minsize(700, 300)
        self.transient(master)
        self.on_done = on_done
        self.rows: dict[str, dict] = {}
        self.backup_var = tk.BooleanVar(value=True)

        tracks = len({c["index"] for c in changes})
        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text=f"Found emojis in {len(changes)} place{'s' if len(changes) != 1 else ''} "
                            f"across {tracks} track{'s' if tracks != 1 else ''}. Untick anything you want to keep.",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")

        table = ttk.Frame(self, padding=(10, 4))
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("use", "field", "before", "after"), show="headings",
                                 selectmode="extended")
        for col, title, width, stretch in (("use", "Use", 45, False), ("field", "Where", 100, False),
                                           ("before", "Before", 400, True), ("after", "After", 400, True)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=stretch, anchor="center" if col == "use" else "w")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for change in changes:
            label = "File name" if change["field"] == FILE_NAME else FIELD_LABELS[change["field"]]
            iid = self.tree.insert("", "end", values=(CHECKED, label, change["before"], change["after"]))
            self.rows[iid] = {**change, "checked": True}
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.selection()))

        bottom = ttk.Frame(self, padding=(10, 4, 10, 10))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Tick all", command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bottom, text="Untick all", command=lambda: self._set_all(False)).pack(side="left", padx=6)
        ttk.Checkbutton(bottom, text=f"Back up original files before changing tags (to a hidden {BACKUP_DIR_NAME} folder)",
                        variable=self.backup_var).pack(side="left", padx=12)
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        self.apply_button = ttk.Button(bottom, command=self.apply)
        self.apply_button.pack(side="right", padx=6)
        self._update_button()

    def _on_click(self, event):
        if self.tree.identify_column(event.x) == "#1" and self.tree.identify_region(event.x, event.y) == "cell":
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle([iid])
                return "break"
        return None

    def _toggle(self, iids, state: bool | None = None):
        for iid in iids:
            row = self.rows[iid]
            row["checked"] = (not row["checked"]) if state is None else state
            self.tree.set(iid, "use", CHECKED if row["checked"] else UNCHECKED)
        self._update_button()
        return "break"

    def _set_all(self, state: bool):
        self._toggle(list(self.rows), state)

    def _update_button(self):
        count = sum(r["checked"] for r in self.rows.values())
        self.apply_button.configure(text=f"Apply {count} change{'s' if count != 1 else ''}",
                                    state="normal" if count else "disabled")

    def apply(self):
        per_track: dict[int, dict] = {}
        for row in self.rows.values():
            if row["checked"]:
                entry = per_track.setdefault(row["index"], {"track": row["track"], "values": {}, "rename": None})
                if row["field"] == FILE_NAME:
                    entry["rename"] = row["after"]
                else:
                    entry["values"][row["field"]] = row["after"]
        errors = []
        for entry in per_track.values():
            track: TrackInfo = entry["track"]
            try:
                if entry["values"]:  # tags first, while the file still has its old name
                    write_tags(track.path, entry["values"], backup=self.backup_var.get())
                if entry["rename"]:
                    new_path = _unique_path(os.path.join(os.path.dirname(track.path), entry["rename"]))
                    os.rename(track.path, new_path)
                    track.path = new_path
            except Exception as exc:
                errors.append(f"{track.filename}: {exc}")
        self.on_done(list(per_track))
        if errors:
            messagebox.showwarning("Some files could not be changed",
                                   f"{len(per_track) - len(errors)} of {len(per_track)} tracks updated.\n\n"
                                   + "\n".join(errors[:15]), parent=self)
        self.destroy()

"""Window for typing one tag value (e.g. a genre) and writing it to many tracks at once."""
from __future__ import annotations

import io
import os
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

from tag_finder import prepare_artwork
from tag_reader import ARTWORK, FIELD_LABELS, TrackInfo
from tag_writer import BACKUP_DIR_NAME, write_tags

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

# Track number and title differ per song, so setting one value for many tracks makes no sense.
EDITABLE_FIELDS = ["genre", "artist", "album", "albumartist", "year", "disc", "composer", ARTWORK]
SINGLE_TRACK_FIELDS = ["title", "track"]  # only offered when one track is selected
REPLACE_ALL, ONLY_EMPTY = "all", "empty"


class SetTagDialog(tk.Toplevel):
    def __init__(self, master, targets: list[tuple[int, TrackInfo]], library: list[TrackInfo],
                 on_written, field: str = "genre"):
        """targets: (track index, track) pairs to edit. library: all scanned tracks, used to
        suggest values already in use. on_written(indices) is called after writing."""
        super().__init__(master)
        self.title("Set tag on selected tracks")
        self.resizable(False, False)
        self.transient(master)

        self.targets = [(i, t) for i, t in targets if not t.error]
        self.library = library
        self.on_written = on_written
        self.artwork: tuple[bytes, str] | None = None
        self._preview_image = None

        self.fields = EDITABLE_FIELDS + (SINGLE_TRACK_FIELDS if len(self.targets) == 1 else [])
        field = field if field in self.fields else "genre"
        self.field_var = tk.StringVar(value=FIELD_LABELS[field])
        self.value_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=REPLACE_ALL)
        self.backup_var = tk.BooleanVar(value=True)
        self.summary_var = tk.StringVar()

        self._build_ui()
        self._field_changed()
        self.value_var.trace_add("write", lambda *a: self._update_summary())
        self.bind("<Return>", lambda e: self.apply())
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(50, self.value_box.focus_set)

    @property
    def field(self) -> str:
        return next(k for k in self.fields if FIELD_LABELS[k] == self.field_var.get())

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        folders = {os.path.dirname(t.path) for _, t in self.targets}
        where = f" in “{os.path.basename(next(iter(folders)))}”" if len(folders) == 1 else ""
        ttk.Label(body, text=f"Set a tag on {len(self.targets)} track{'s' if len(self.targets) != 1 else ''}{where}",
                  font=("TkDefaultFont", 13, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(body, text="Tag:").grid(row=1, column=0, sticky="w")
        field_box = ttk.Combobox(body, textvariable=self.field_var, state="readonly", width=28,
                                 values=[FIELD_LABELS[k] for k in self.fields])
        field_box.grid(row=1, column=1, columnspan=2, sticky="we", pady=3)
        field_box.bind("<<ComboboxSelected>>", lambda e: self._field_changed())

        ttk.Label(body, text="Value:").grid(row=2, column=0, sticky="nw", pady=3)
        self.value_box = ttk.Combobox(body, textvariable=self.value_var, width=28)
        self.value_box.grid(row=2, column=1, columnspan=2, sticky="we", pady=3)
        self.art_frame = ttk.Frame(body)
        self.art_frame.grid(row=2, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Button(self.art_frame, text="Choose image…", command=self._choose_image).pack(side="left")
        self.art_preview = ttk.Label(self.art_frame, text="  No image chosen")
        self.art_preview.pack(side="left", padx=8)

        self.hint = ttk.Label(body, foreground="#777777")
        self.hint.grid(row=3, column=1, columnspan=2, sticky="w")

        ttk.Label(body, text="Apply to:").grid(row=4, column=0, sticky="nw", pady=(10, 0))
        ttk.Radiobutton(body, text="All selected tracks (replace existing values)", value=REPLACE_ALL,
                        variable=self.mode_var, command=self._update_summary).grid(row=4, column=1, columnspan=2,
                                                                                    sticky="w", pady=(10, 0))
        ttk.Radiobutton(body, text="Only tracks where it is empty", value=ONLY_EMPTY,
                        variable=self.mode_var, command=self._update_summary).grid(row=5, column=1, columnspan=2,
                                                                                    sticky="w")

        ttk.Label(body, textvariable=self.summary_var, wraplength=380, justify="left").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Checkbutton(body, text=f"Back up original files (to a hidden {BACKUP_DIR_NAME} folder)",
                        variable=self.backup_var).grid(row=7, column=0, columnspan=3, sticky="w")

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        self.apply_button = ttk.Button(buttons, text="Write", command=self.apply)
        self.apply_button.pack(side="right", padx=6)

    def _field_changed(self):
        field = self.field
        if field == ARTWORK:
            self.value_box.grid_remove()
            self.art_frame.grid()
            self.hint.configure(text="Tip: select a whole album folder and give every track the same cover.")
        else:
            self.art_frame.grid_remove()
            self.value_box.grid()
            # Offer values already used in the library, most common first, plus this selection's current value.
            counts = (Counter() if field in SINGLE_TRACK_FIELDS  # titles aren't shared between songs
                      else Counter(t.tags[field] for t in self.library if field in t.tags))
            self.value_box.configure(values=[v for v, _ in counts.most_common()])
            current = {t.tags[field] for _, t in self.targets if field in t.tags}
            self.value_var.set(next(iter(current)) if len(current) == 1 else "")  # pre-fill only a shared value
            self.hint.configure(text="Type a new value or pick one already used in your library."
                                if counts else "Type the value to write.")
        self._update_summary()

    def _choose_image(self):
        path = filedialog.askopenfilename(parent=self, title="Choose cover image",
                                          filetypes=[("Images", "*.jpg *.jpeg *.png"), ("All files", "*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                data, mime, info = prepare_artwork(fh.read())
        except Exception as exc:
            messagebox.showerror("Could not use image", str(exc), parent=self)
            return
        self.artwork = (data, mime)
        self._preview_image = None
        if Image is not None:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((64, 64))
            self._preview_image = ImageTk.PhotoImage(img)
        self.art_preview.configure(image=self._preview_image or "", compound="left",
                                   text=f"  {os.path.basename(path)} ({info or mime})")
        self._update_summary()

    # ---------------------------------------------------------------- planning & writing
    def _plan(self) -> tuple[list[tuple[int, TrackInfo]], dict[str, int]]:
        """Tracks that will be written, plus counts of empty / different / already-equal tracks."""
        field, value = self.field, self.value_var.get().strip()
        counts = {"empty": 0, "different": 0, "same": 0}
        plan = []
        for index, track in self.targets:
            if field == ARTWORK:
                state = "different" if track.has_artwork else "empty"
            else:
                current = track.tags.get(field)
                state = "empty" if not current else "same" if current == value else "different"
            counts[state] += 1
            if state == "empty" or (state == "different" and self.mode_var.get() == REPLACE_ALL):
                plan.append((index, track))
        return plan, counts

    def _update_summary(self):
        field = self.field
        label = FIELD_LABELS[field].lower()
        has_value = self.artwork is not None if field == ARTWORK else bool(self.value_var.get().strip())
        plan, counts = self._plan()
        parts = []
        if counts["empty"]:
            parts.append(f"{counts['empty']} have no {label}")
        if counts["different"]:
            other = "artwork" if field == ARTWORK else f"a different {label}"
            action = "will be replaced" if self.mode_var.get() == REPLACE_ALL else "will be left alone"
            parts.append(f"{counts['different']} already have {other} ({action})")
        if counts["same"]:
            parts.append(f"{counts['same']} already have this value")
        summary = "; ".join(parts) + "." if parts else ""
        if has_value:
            summary += f"\n→ {len(plan)} file{'s' if len(plan) != 1 else ''} will be changed."
        self.summary_var.set(summary)
        self.apply_button.configure(text=f"Write to {len(plan)} file{'s' if len(plan) != 1 else ''}",
                                    state="normal" if has_value and plan else "disabled")

    def apply(self):
        field, value = self.field, self.value_var.get().strip()
        plan, counts = self._plan()
        if not plan or (field == ARTWORK and self.artwork is None) or (field != ARTWORK and not value):
            return
        replacing = counts["different"] if self.mode_var.get() == REPLACE_ALL else 0
        if replacing and not messagebox.askyesno(
                "Replace existing values?",
                f"{replacing} of these tracks already have a different {FIELD_LABELS[field].lower()}, "
                f"which will be overwritten. Continue?", parent=self):
            return

        self.apply_button.configure(state="disabled")
        errors = []
        for n, (_, track) in enumerate(plan, 1):
            self.summary_var.set(f"Writing {n} of {len(plan)}: {track.filename}")
            self.update_idletasks()
            try:
                if field == ARTWORK:
                    write_tags(track.path, {}, self.artwork, backup=self.backup_var.get())
                else:
                    write_tags(track.path, {field: value}, backup=self.backup_var.get())
            except Exception as exc:
                errors.append(f"{track.filename}: {exc}")
        self.on_written([index for index, _ in plan])
        if errors:
            messagebox.showwarning("Some files could not be written",
                                   f"{len(plan) - len(errors)} of {len(plan)} files updated.\n\n"
                                   + "\n".join(errors[:15]), parent=self)
        self.destroy()

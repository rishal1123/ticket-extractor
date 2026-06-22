"""View layer: the tkinter UI.

Pure presentation — it knows nothing about ISPs, formatting, or clipboards.
It exposes:
  * callback hooks the controller assigns: ``on_change``, ``on_copy``, ``on_clear``
  * methods the controller calls to drive the UI: ``get_input``, ``set_output``,
    ``set_status``, ``render_manual_fields``, ``mark_missing``, ``get_manual_values``.
"""

from __future__ import annotations

import re
import tkinter as tk
from tkinter import ttk, scrolledtext
from typing import Callable

_STATUS_COLORS = {
    "info": "#666",
    "ok": "#0a8a0a",
    "warn": "#b88000",
    "error": "#b00000",
}
_RED = "#d00000"
_MONO = ("Consolas", 10)


def _noop(*_a, **_k):
    pass


class AppView(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ISP Ticket Formatter")
        self.geometry("900x700")
        self.minsize(640, 480)

        # Controller-assigned callbacks.
        self.on_change: Callable[[], None] = _noop
        self.on_copy: Callable[[], None] = _noop
        self.on_clear: Callable[[], None] = _noop

        self.manual_vars: dict[str, tk.StringVar] = {}
        self.manual_labels: dict[str, ttk.Label] = {}
        self._manual_keys: tuple[str, ...] = ()
        self._debounce_id = None

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        ttk.Label(
            root,
            text="Paste the unformatted ticket data below — it formats automatically.",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        ttk.Label(root, text="Pasted data (input):").pack(anchor="w")
        self.input_box = scrolledtext.ScrolledText(root, height=12, wrap="word", font=_MONO)
        self.input_box.pack(fill="both", expand=True, pady=(2, 8))
        self.input_box.bind("<<Paste>>", lambda e: self.after(10, self._fire_change))
        self.input_box.bind("<KeyRelease>", lambda e: self._debounced_change())

        # Manual-entry fields (built dynamically per detected ISP).
        self.manual_frame = ttk.Frame(root)

        bar = self.bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(0, 8))
        self.status = ttk.Label(bar, text="Waiting for paste…", foreground=_STATUS_COLORS["info"])
        self.status.pack(side="left")
        ttk.Button(bar, text="Copy output", command=lambda: self.on_copy()).pack(side="right")
        ttk.Button(bar, text="Clear", command=lambda: self.on_clear()).pack(
            side="right", padx=(0, 6))

        ttk.Label(root, text="Formatted (output) — auto-copied to clipboard:").pack(anchor="w")
        self.output_box = scrolledtext.ScrolledText(root, height=14, wrap="word", font=_MONO)
        self.output_box.pack(fill="both", expand=True, pady=(2, 0))
        self.output_box.tag_configure("bold", font=("Consolas", 10, "bold"))
        self.output_box.tag_configure("manual", foreground=_RED, font=("Consolas", 10, "bold"))

    # ----------------------------------------------------------- events
    def _debounced_change(self):
        if self._debounce_id is not None:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(250, self._fire_change)

    def _fire_change(self):
        self._debounce_id = None
        self.on_change()

    # --------------------------------------------------- controller API
    def get_input(self) -> str:
        return self.input_box.get("1.0", "end").strip()

    def get_output(self) -> str:
        return self.output_box.get("1.0", "end").strip()

    def get_manual_values(self) -> dict:
        return {k: v.get() for k, v in self.manual_vars.items()}

    def set_status(self, text: str, level: str = "info"):
        self.status.config(text=text, foreground=_STATUS_COLORS.get(level, _STATUS_COLORS["info"]))

    def set_output(self, text: str):
        self.output_box.delete("1.0", "end")
        self.output_box.insert("1.0", text)
        self._bold_heading()
        self._highlight_placeholders()

    def render_manual_fields(self, fields: tuple):
        """Show the labeled boxes for the detected ISP, preserving typed values."""
        keys = tuple(k for k, _ in fields)
        if keys == self._manual_keys:
            return  # unchanged — keep what the user typed

        old_values = self.get_manual_values()
        for child in self.manual_frame.winfo_children():
            child.destroy()
        self.manual_vars.clear()
        self.manual_labels.clear()
        self._manual_keys = keys

        if not fields:
            self.manual_frame.pack_forget()
            return

        for key, label in fields:
            var = tk.StringVar(value=old_values.get(key, ""))
            var.trace_add("write", lambda *a: self._debounced_change())
            self.manual_vars[key] = var
            lbl = ttk.Label(self.manual_frame, text=f"{label}:")
            lbl.pack(side="left")
            self.manual_labels[key] = lbl
            ttk.Entry(self.manual_frame, textvariable=var, width=20).pack(side="left", padx=(6, 14))

        if not self.manual_frame.winfo_ismapped():
            self.manual_frame.pack(fill="x", pady=(0, 6), before=self.bar)

    def mark_missing(self, missing_keys: tuple):
        """Colour the labels of still-empty manual fields red."""
        for key, lbl in self.manual_labels.items():
            lbl.config(foreground=_RED if key in missing_keys else "")

    def clear(self):
        self.input_box.delete("1.0", "end")
        self.output_box.delete("1.0", "end")

    def copy_plain(self, text: str):
        """Plain-text clipboard fallback (used when rich copy is unavailable)."""
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()

    # ------------------------------------------------------- rendering
    def _bold_heading(self):
        self.output_box.tag_remove("bold", "1.0", "end")
        lines = self.output_box.get("1.0", "end-1c").split("\n")
        bolded = 0
        for idx, line in enumerate(lines, start=1):
            if line.strip():
                self.output_box.tag_add("bold", f"{idx}.0", f"{idx}.end")
                bolded += 1
                if bolded == 2:
                    break

    def _highlight_placeholders(self):
        self.output_box.tag_remove("manual", "1.0", "end")
        lines = self.output_box.get("1.0", "end-1c").split("\n")
        for i, line in enumerate(lines, start=1):
            for m in re.finditer(r"<[^>\n]+>", line):
                self.output_box.tag_add("manual", f"{i}.{m.start()}", f"{i}.{m.end()}")
"""Controller layer: mediates between the view and the model.

Holds no UI or parsing logic itself. On any input/manual-field change it asks
the model to format, then pushes the result into the view and copies to the
clipboard once everything required is filled in.

It depends only on the *interfaces* of the view, model, and clipboard service,
so it can be driven headless in tests with fakes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AppController:
    def __init__(self, model, view, clipboard):
        self.model = model
        self.view = view
        self.clipboard = clipboard

        # Subscribe to the view's events.
        view.on_change = self.handle_change
        view.on_copy = self.handle_copy
        view.on_clear = self.handle_clear

    # ------------------------------------------------------------- events
    def handle_change(self):
        raw = self.view.get_input()
        manual = self.view.get_manual_values()
        result = self.model.build(raw, manual)

        self.view.render_manual_fields(result.manual_fields)
        self.view.mark_missing(result.missing_keys)
        self.view.set_output(result.text)

        if not raw:
            self.view.set_status("Waiting for paste…", "info")
        elif not result.detected:
            self.view.set_status("Could not detect ISP.", "error")
        elif result.missing_keys:
            labels = ", ".join(result.missing_labels())
            self.view.set_status(f"Detected: {result.isp}  •  enter {labels} above ⤴", "warn")
        else:
            self._copy(result.text)
            self.view.set_status(
                f"Detected: {result.isp}  •  output copied to clipboard ✓", "ok")

    def handle_copy(self):
        text = self.view.get_output()
        if text:
            self._copy(text)
            self.view.set_status("Output copied to clipboard ✓", "ok")

    def handle_clear(self):
        self.view.clear()
        self.view.render_manual_fields(())
        self.view.set_status("Waiting for paste…", "info")

    # ------------------------------------------------------------- helpers
    def _copy(self, text: str):
        # Prefer rich (HTML) copy so the bold heading survives the paste;
        # fall back to plain text where the rich clipboard is unavailable.
        if not self.clipboard.copy(text):
            logger.info("Rich clipboard copy unavailable; using plain-text fallback")
            self.view.copy_plain(text)
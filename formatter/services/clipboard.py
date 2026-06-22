"""Windows rich-text clipboard service.

Puts BOTH ``CF_HTML`` (rich, read by Outlook / Gmail / Word / browsers) and
``CF_UNICODETEXT`` (plain-text fallback) on the clipboard, so the destination
app picks the richest format it understands and bold survives the paste.

Pure ctypes — no third-party dependencies. On non-Windows platforms the
clipboard is unavailable and :meth:`ClipboardService.copy` returns False so the
caller can fall back to a plain-text path.
"""

from __future__ import annotations

import html as _html
import logging
import re

logger = logging.getLogger(__name__)

# Matches an http(s) URL in already-HTML-escaped text. Escaping turns "<"/">"/'"'
# into entities, so a bare URL run is just non-whitespace up to one of those.
# Trailing sentence punctuation is excluded so "see https://x.com." stays clean.
_URL_RE = re.compile(r"https?://[^\s<>\"']+[^\s<>\"'.,;:!?)]")

try:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    # Pointer-correct signatures (avoids handle truncation on 64-bit).
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = wintypes.LPVOID
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT
    _user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]

    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002
    _CF_HTML = _user32.RegisterClipboardFormatW("HTML Format")
    _AVAILABLE = True
except Exception:  # pragma: no cover - non-Windows / no clipboard
    _AVAILABLE = False


def _linkify(escaped: str) -> str:
    """Wrap http(s) URLs in an already-escaped string with clickable <a> tags."""
    return _URL_RE.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', escaped)


def text_to_html(text: str, bold_lines: int = 2) -> str:
    """Render plain text as an HTML fragment, bolding the first N non-empty lines.

    URLs are wrapped in <a> tags so they stay clickable when pasted into Outlook,
    Gmail, Word, etc.
    """
    out, bolded, links = [], 0, 0
    for line in text.split("\n"):
        esc = _linkify(_html.escape(line))
        links += esc.count("<a href")
        if line.strip() and bolded < bold_lines:
            out.append(f"<b>{esc}</b>")
            bolded += 1
        else:
            out.append(esc)
    logger.debug("text_to_html: %d line(s), %d link(s)", len(out), links)
    return "<div>" + "<br>".join(out) + "</div>"


def _wrap_cf_html(fragment: str) -> bytes:
    """Wrap an HTML fragment in the CF_HTML clipboard envelope (with byte offsets)."""
    header = (
        "Version:0.9\r\n"
        "StartHTML:{:010d}\r\n"
        "EndHTML:{:010d}\r\n"
        "StartFragment:{:010d}\r\n"
        "EndFragment:{:010d}\r\n"
    )
    pre = "<html><body>\r\n<!--StartFragment-->"
    post = "<!--EndFragment-->\r\n</body></html>"

    head_len = len(header.format(0, 0, 0, 0).encode("utf-8"))
    start_fragment = head_len + len(pre.encode("utf-8"))
    end_fragment = start_fragment + len(fragment.encode("utf-8"))
    end_html = end_fragment + len(post.encode("utf-8"))

    full = header.format(head_len, end_html, start_fragment, end_fragment) + pre + fragment + post
    return full.encode("utf-8")


def _set_clipboard(entries: list) -> bool:
    """Place each (format_id, bytes) onto the clipboard in one session."""
    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        for fmt_id, data in entries:
            size = len(data)
            handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                _kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, data, size)
            _kernel32.GlobalUnlock(handle)
            if not _user32.SetClipboardData(fmt_id, handle):
                _kernel32.GlobalFree(handle)  # ownership not transferred
                return False
        return True
    finally:
        _user32.CloseClipboard()


class ClipboardService:
    """Copies rich (HTML) + plain text to the system clipboard."""

    available = _AVAILABLE

    def copy(self, text: str, bold_lines: int = 2) -> bool:
        """Copy ``text`` as rich HTML + plain text. Returns False if unavailable."""
        if not _AVAILABLE:
            logger.debug("Rich clipboard unavailable; caller should fall back to plain text")
            return False
        fragment = text_to_html(text, bold_lines=bold_lines)
        plain_bytes = text.encode("utf-16-le") + b"\x00\x00"
        html_bytes = _wrap_cf_html(fragment) + b"\x00"
        ok = _set_clipboard([(_CF_HTML, html_bytes), (_CF_UNICODETEXT, plain_bytes)])
        if not ok:
            logger.warning("Rich clipboard copy failed; caller should fall back to plain text")
        return ok
"""Adapter that reuses the standalone formatter's domain layer (formatter/model)
to turn a captured raw ISP detail-page dump into a standardized ticket block.

Kept thin on purpose: detection + formatting live in formatter/model/formatters.py
and are shared with the standalone formatter app, so output stays identical.
"""

from __future__ import annotations

from typing import Optional

from utils.logger import get_logger

logger = get_logger("ticket_formatter")

# Map our portal keys to the keyword the formatter detects on, so detection is
# reliable even when the captured dump doesn't literally contain the ISP name.
_PORTAL_KEYWORD = {
    "dhiraagu": "dhiraagu",
    "ooredoo": "ooredoo",
    "medianet": "medianet",
    "rol": "raajje online",
}


def format_ticket_dump(raw_dump: Optional[str], portal: str, manual: Optional[dict] = None) -> dict:
    """Format a raw dump into a standardized block.

    Returns a dict: {ok, isp, text, missing, warnings, error}. ``ok`` is False
    when there's no dump or the ISP couldn't be formatted.
    """
    if not raw_dump or not raw_dump.strip():
        return {"ok": False, "isp": None, "text": "", "missing": [], "warnings": [],
                "error": "No captured portal data for this ticket yet."}

    try:
        from formatter.model import TicketModel
    except Exception as e:  # pragma: no cover - import/setup issue
        logger.error(f"Formatter model import failed: {e}")
        return {"ok": False, "isp": None, "text": "", "missing": [], "warnings": [],
                "error": f"Formatter unavailable: {e}"}

    # Prefix the portal keyword so detect_formatter() reliably picks the right ISP.
    keyword = _PORTAL_KEYWORD.get((portal or "").lower(), portal or "")
    raw = f"{keyword}\n{raw_dump}"

    result = TicketModel().build(raw, manual or {})
    if not result.detected:
        return {"ok": False, "isp": None, "text": result.text or "", "missing": [],
                "warnings": [], "error": "Could not detect the ISP from the captured data."}

    return {
        "ok": True,
        "isp": result.isp,
        "text": result.text,
        "missing": list(result.missing_labels()),
        "warnings": list(result.warnings),
        "error": None,
    }

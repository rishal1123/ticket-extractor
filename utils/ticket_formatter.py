"""Adapter that reuses the standalone formatter's domain layer (formatter/model)
to turn a captured raw ISP detail-page dump into a standardized ticket block.

Kept thin on purpose: detection + formatting live in formatter/model/formatters.py
and are shared with the standalone formatter app, so output stays identical.
"""

from __future__ import annotations

import re
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


def _load_address_rules() -> Optional[dict]:
    """Load the formatter's admin-editable address/atoll validation rules."""
    try:
        from formatter.services import rules_store
        return rules_store.load()
    except Exception as e:  # pragma: no cover
        logger.warning(f"Could not load address rules: {e}")
        return None


def _get_znuny():
    """Return a Znuny client for the formatter's cross-checks (verify_address /
    account_exists), or None when Znuny isn't configured. Reuses the app's own
    ZnunyClient against the same instance."""
    try:
        from config import Config
        if Config.get_znuny_url() and Config.get_znuny_username() and Config.get_znuny_password():
            from znuny_client import ZnunyClient
            return ZnunyClient()
    except Exception as e:  # pragma: no cover
        logger.warning(f"Znuny verification unavailable: {e}")
    return None


def _get_nocbot():
    """Return a NocBotService for relocation account lookups (see
    NocBotService.lookup_relocation_data), or None when NocBot isn't
    configured. NocBot's service-search is DB-only (no live SMX round trip),
    so unlike the Znuny cross-check this is left on regardless of
    verify_with_znuny -- it's only attempted for tickets whose portal
    ticket_type is "Relocation" anyway, not every ticket."""
    try:
        from services.nocbot_service import NocBotService
        service = NocBotService()
        return service if service.enabled else None
    except Exception as e:  # pragma: no cover
        logger.warning(f"NocBot lookup unavailable: {e}")
        return None


def manual_from_ticket(ticket) -> dict:
    """Auto-fill the formatter's manual fields from a ticket where we can.

    Dhiraagu's only manual field is "Order URL ID" — the /orders/hdc/{id} segment
    of the order URL. Derive it from the ticket's portal_url, falling back to the
    ticket_id (the extractor builds the detail URL from ticket_id anyway).
    """
    manual: dict = {}
    portal = (getattr(ticket, "portal", "") or "").lower()
    if portal == "dhiraagu":
        order_url_id = None
        portal_url = getattr(ticket, "portal_url", None)
        if portal_url:
            m = re.search(r"/orders/hdc/([^/?#]+)", portal_url)
            if m:
                order_url_id = m.group(1)
        order_url_id = order_url_id or getattr(ticket, "ticket_id", None)
        if order_url_id:
            manual["order_url_id"] = str(order_url_id)
    return manual


def extract_phone(raw_dump: Optional[str], portal: str) -> Optional[str]:
    """Pull the customer's contact number out of a ticket's captured raw dump,
    reusing the same per-portal parsing the standalone formatter already does
    for its "Phone :" output line — just exposed here without needing to
    generate the full formatted block."""
    if not raw_dump or not raw_dump.strip():
        return None
    try:
        from formatter.model.formatters import detect_formatter
    except Exception as e:  # pragma: no cover - import/setup issue
        logger.error(f"Formatter model import failed: {e}")
        return None

    keyword = _PORTAL_KEYWORD.get((portal or "").lower(), portal or "")
    raw = f"{keyword}\n{raw_dump}"
    fmt = detect_formatter(raw)
    if fmt is None:
        return None
    try:
        return fmt.phone_for_display(raw)
    except Exception as e:  # pragma: no cover - defensive, parsing is regex-only
        logger.warning(f"Phone extraction failed for {portal}: {e}")
        return None


def format_ticket_dump(raw_dump: Optional[str], portal: str, manual: Optional[dict] = None,
                       relocation: Optional[bool] = None, verify_with_znuny: bool = True,
                       ticket_type: Optional[str] = None) -> dict:
    """Format a raw dump into a standardized block.

    Returns a dict: {ok, isp, text, missing, warnings, relocation_possible,
    relocation, error}. ``ok`` is False when there's no dump or the ISP couldn't be
    formatted.

    ``relocation``: None (default) auto-decides -- when ``ticket_type`` is
    "Relocation" (as reported by the ISP portal), NocBot is checked for an
    existing service on the account; found -> format as Relocation with the
    old-service fields auto-filled, not found -> format as New Service instead
    (the portal's own type label isn't always reliable). Pass True/False to
    force one or the other regardless of ticket_type (e.g. the formatter
    page's manual "Convert to Relocation" toggle).

    ``verify_with_znuny=False`` skips the live Znuny cross-check (address/account
    lookups) -- those are real network round trips per ticket, fine for a single
    formatter-page view but too slow when formatting many tickets at once (e.g.
    the external API's bulk export). The NocBot relocation lookup is unaffected
    by this flag (DB-only on NocBot's side, and only attempted for tickets
    actually typed "Relocation").
    """
    if not raw_dump or not raw_dump.strip():
        return {"ok": False, "isp": None, "text": "", "missing": [], "warnings": [],
                "relocation_possible": False, "relocation": False,
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

    # Apply the same verifications the standalone formatter does:
    #  - address/atoll format rules (pure, admin-configurable JSON)
    #  - Znuny cross-checks (address exists as CustomerID; account relocation)
    result = TicketModel().build(
        raw, manual or {},
        address_rules=_load_address_rules(),
        znuny=_get_znuny() if verify_with_znuny else None,
        relocation=relocation,
        ticket_type=ticket_type,
        nocbot=_get_nocbot(),
    )
    if not result.detected:
        return {"ok": False, "isp": None, "text": result.text or "", "missing": [],
                "warnings": [], "relocation_possible": False, "relocation": False,
                "error": "Could not detect the ISP from the captured data."}

    # Rich HTML (bold heading + clickable URLs), same as the standalone formatter,
    # so the Copy button can put rich text on the clipboard.
    try:
        from formatter.services.clipboard import text_to_html
        html = text_to_html(result.text)
    except Exception:
        html = ""

    return {
        "ok": True,
        "isp": result.isp,
        "text": result.text,
        "html": html,
        "missing": list(result.missing_labels()),
        "warnings": list(result.warnings),
        "relocation_possible": result.relocation_possible,
        "relocation": result.relocation,
        "error": None,
    }

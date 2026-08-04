"""ISP ticket formatters — the domain layer.

Each ISP has a formatter that:
  1. is auto-detected from the pasted (unformatted) dashboard dump,
  2. parses the relevant fields out of that dump,
  3. renders a clean, standardized ticket block (and ticket URL).

Add a new ISP by subclassing ``BaseFormatter`` and registering it in
``ALL_FORMATTERS`` at the bottom of this file.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Optional


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _lines(text: str) -> list[str]:
    """Return the dump split into stripped, list-indexed lines."""
    return [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]


def _normalize_label(label: str) -> str:
    """Lowercase a label and drop a trailing required-field marker (' *')."""
    return re.sub(r"\s*\*+\s*$", "", label).strip().lower()


def field_after_label(text: str, label: str) -> Optional[str]:
    """Find an exact label line and return the next non-empty line's value.

    The dashboard dumps put a field name on its own line, then the value a
    few (blank) lines later, e.g.::

        Account Number

        40102206

    Matching the label *exactly* avoids confusing e.g. "Ticket ID" with
    "Salesforce Ticket ID" or "Contact" with "Customer Contact".
    """
    lines = _lines(text)
    target = _normalize_label(label)
    for i, line in enumerate(lines):
        if _normalize_label(line) == target:
            for nxt in lines[i + 1:]:
                if nxt:
                    return nxt
    return None


def lines_after_label(text: str, label: str, count: int) -> list[str]:
    """Return the next ``count`` non-empty lines after an exact label line."""
    lines = _lines(text)
    target = _normalize_label(label)
    for i, line in enumerate(lines):
        if _normalize_label(line) == target:
            found = [ln for ln in lines[i + 1:] if ln]
            return found[:count]
    return []


def build_address(*parts: Optional[str]) -> str:
    """Join non-empty address parts with '-', e.g. UD-05 / 12 / 02 -> UD-05-12-02."""
    return "-".join(p.strip() for p in parts if p and p.strip())


def manual_value(manual: Optional[dict], key: str, label: str) -> str:
    """Return the typed manual value, or a ``<enter Label>`` placeholder if missing.

    The view highlights ``<...>`` placeholders in red so it's obvious what's
    still required.
    """
    val = (manual or {}).get(key, "").strip()
    return val if val else f"<enter {label}>"


def old_value(old_service: Optional[dict], key: str, label: str) -> str:
    """Same convention as manual_value(), for a relocation ticket's "old
    service" fields auto-filled from NocBot (see NocBotService.
    lookup_relocation_data) instead of typed by hand: the real value if
    NocBot had it, else the same ``<enter Label>`` placeholder used for
    manual fields.
    """
    val = ((old_service or {}).get(key) or "").strip()
    return val if val else f"<enter {label}>"


def split_name_account(value: Optional[str]) -> tuple[str, str]:
    """Split "Shihaaz Shamoon (5481835352241408)" -> ("Shihaaz Shamoon", "5481835352241408")."""
    if not value:
        return "", ""
    m = re.match(r"\s*(.*?)\s*\(([^)]*)\)", value)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return value.strip(), ""


# Address-type tags that get prepended to the building code in some systems.
_ADDRESS_TAGS = ("HOME", "OFFICE", "WORK", "BUSINESS", "BILLING", "SHIPPING")


def clean_building_code(value: Optional[str]) -> str:
    """Extract the building code: drop a leading address tag, keep up to first comma.

    "HOMEUD-05-02-06, Neighborhood 4, ..." -> "UD-05-02-06"
    """
    if not value:
        return ""
    head = value.split(",")[0].strip()
    head = re.sub(rf"^({'|'.join(_ADDRESS_TAGS)})", "", head, flags=re.IGNORECASE)
    return head.strip()


def dedupe_name(name: Optional[str]) -> str:
    """Collapse a doubled full name ("Adil Ibrahim Adil Ibrahim" -> "Adil Ibrahim")."""
    if not name:
        return ""
    words = name.split()
    n = len(words)
    if n % 2 == 0 and words[: n // 2] == words[n // 2:]:
        words = words[: n // 2]
    return " ".join(words)


def local_phone(contact: Optional[str]) -> str:
    """Normalise a Maldives contact number to its 7-digit local form.

    Strips a leading 960 country code (and any +, spaces, dashes).
    "9607669592" -> "7669592"
    """
    if not contact:
        return ""
    digits = re.sub(r"\D", "", contact)
    if len(digits) > 7 and digits.startswith("960"):
        digits = digits[3:]
    return digits


def looks_like_email(line: str) -> bool:
    """True for an email-address line (e.g. a stray contact email in a block)."""
    return "@" in line


def looks_like_phone(line: str) -> bool:
    """True for a phone-number line: only +, digits, spaces, dashes, parens; >=7 digits."""
    line = line.strip()
    return bool(re.fullmatch(r"[+\d][\d\s\-()]*", line)) and len(re.sub(r"\D", "", line)) >= 7


def matches_any(value: str, patterns) -> bool:
    """True if ``value`` matches any glob ``pattern`` (``*`` wildcard, case-insensitive)."""
    v = (value or "").strip().upper()
    return any(fnmatch.fnmatchcase(v, str(p).strip().upper()) for p in (patterns or []))


def validate_value(label: str, value: str, allowed) -> Optional[str]:
    """Check a plain field value against an allowed set (e.g. Atoll == "Kaafu").

    Returns a warning string if ``value`` matches none of ``allowed``, else None.
    With no ``allowed`` configured, nothing is checked.
    """
    if not allowed:
        return None
    if matches_any(value, allowed):
        return None
    shown = (value or "").strip() or "(none)"
    return f'{label} "{shown}" is not valid (expected: {", ".join(allowed)})'


def validate_address(zone: str, address_id: str, zone_rules: Optional[dict]) -> Optional[str]:
    """Check ``address_id`` against the allowed patterns for ``zone``.

    ``zone_rules`` maps a zone (e.g. an Ooredoo street) to a list of glob patterns
    (``*`` wildcard, case-insensitive), e.g. ``{"Phase 1": ["TGR*", "R1B*"]}``.

    Returns a warning string, or ``None`` when everything checks out. Two ways to
    warn, both for an ISP that has zone rules configured:
      * the zone isn't one of the configured zones (e.g. an incomplete street
        like "Phase" instead of "Phase 1"/"Phase 2"), or
      * the zone is recognized but the address doesn't match its patterns.

    When there are *no* zone rules (``zone_rules`` empty), nothing is checked.
    """
    if not zone_rules:
        return None

    known = [k for k in zone_rules if str(k).strip()]
    zone = (zone or "").strip()
    address_id = (address_id or "").strip()

    # Case-insensitive zone lookup among the configured zones.
    patterns = next(
        (v for k, v in zone_rules.items() if k.strip().lower() == zone.lower()),
        None,
    )
    if patterns is None:
        # Zone is present but unrecognized (or missing) — can't verify the address.
        shown = zone or "(none)"
        return (
            f'Street/zone "{shown}" is not recognized '
            f'(expected one of: {", ".join(known)})'
        )

    if address_id and matches_any(address_id, patterns):
        return None
    return (
        f'Address "{address_id}" is not a valid {zone} format '
        f'(expected: {", ".join(patterns)})'
    )


# --------------------------------------------------------------------------- #
# Base formatter
# --------------------------------------------------------------------------- #
class BaseFormatter:
    name: str = "Unknown"
    # Substrings whose presence in the dump signals this ISP.
    detect_keywords: tuple[str, ...] = ()
    # Values that aren't in the pasted data and must be typed by the user.
    # Each entry is (key, label) and becomes a labeled box in the view.
    manual_fields: tuple[tuple[str, str], ...] = ()

    def detect_score(self, text: str) -> int:
        """How strongly this formatter matches the dump (higher = better)."""
        low = text.lower()
        return sum(1 for kw in self.detect_keywords if kw.lower() in low)

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        raise NotImplementedError

    def address_for_validation(self, text: str) -> Optional[tuple[str, str]]:
        """Return ``(zone, address_id)`` to validate, or None if N/A for this ISP.

        ``zone`` selects which rule set applies (e.g. the Ooredoo street); the
        address_id is checked against that zone's allowed patterns.
        """
        return None

    def address_id_for_validation(self, text: str) -> Optional[str]:
        """Return just the address/building ID, used to cross-check against Znuny."""
        return None

    def account_id_for_validation(self, text: str) -> Optional[str]:
        """Return the Account # used to detect a possible relocation in Znuny."""
        return None

    def atoll_for_validation(self, text: str) -> Optional[str]:
        """Return the Atoll value to validate, or None if N/A for this ISP."""
        return None

    def phone_for_display(self, text: str) -> Optional[str]:
        """Return the customer's contact number, or None if N/A/not found."""
        return None


class _StubFormatter(BaseFormatter):
    """Placeholder for an ISP whose format hasn't been supplied yet."""

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        return (
            f"[{self.name}] formatter not configured yet.\n\n"
            f"Send a sample (unformatted input + desired output) and this "
            f"ISP will be wired up just like Ooredoo."
        )


# --------------------------------------------------------------------------- #
# Ooredoo
# --------------------------------------------------------------------------- #
class OoredooFormatter(BaseFormatter):
    name = "Ooredoo"
    detect_keywords = ("ooredoo.mv", "ooredoo", "supernet", "ratepan")

    TICKET_URL = "https://www.ooredoo.mv/webapps/FMS/public/tickets/ticket_info/{ticket_id}"

    # Ticket Type -> service label used in the heading. "relocation" maps here
    # too: reaching this (non-relocation) branch with a portal Ticket Type of
    # "Relocation" only happens when NocBot found no existing service for the
    # account, so this ticket is being deliberately treated as New Service --
    # the heading should say that, not parrot the portal's stale label.
    SERVICE_LABELS = {
        "installation": "New Service",
        "relocation": "New Service",
    }

    def _service_label(self, text: str) -> str:
        ttype = field_after_label(text, "Ticket Type") or ""
        return self.SERVICE_LABELS.get(ttype.strip().lower(), ttype.strip() or "New Service")

    def address_id_for_validation(self, text: str) -> Optional[str]:
        return (field_after_label(text, "Address") or "").strip() or None

    def account_id_for_validation(self, text: str) -> Optional[str]:
        return (field_after_label(text, "Account Number") or "").strip() or None

    def address_for_validation(self, text: str) -> Optional[tuple[str, str]]:
        # The "Street" (e.g. "Phase 1"/"Phase 2") selects the rule set; the
        # "Address" field is the building/address ID being validated. Validate
        # whenever there's an address — an empty/incomplete street then surfaces
        # as an "unrecognized zone" warning rather than being silently skipped.
        street = (field_after_label(text, "Street") or "").strip()
        address = self.address_id_for_validation(text)
        if address:
            return street, address
        return None

    def atoll_for_validation(self, text: str) -> Optional[str]:
        # The dashboard labels this "Atoll" on some tickets and "Atol" on others.
        val = field_after_label(text, "Atoll")
        if val is None:
            val = field_after_label(text, "Atol")
        return val

    def phone_for_display(self, text: str) -> Optional[str]:
        return local_phone(field_after_label(text, "Contact")) or None

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        account = field_after_label(text, "Account Number") or ""
        bandwidth = field_after_label(text, "Ratepan") or ""
        name = dedupe_name(field_after_label(text, "Name"))
        phone = local_phone(field_after_label(text, "Contact"))
        address = field_after_label(text, "Address") or ""
        ticket_id = field_after_label(text, "Ticket ID") or ""
        fsan = field_after_label(text, "HDC ONT FSAN") or ""
        ticket_url = self.TICKET_URL.format(ticket_id=ticket_id)

        if relocation:
            old_vlan = ""
            if old_service and (old_service.get("old_svlan") or old_service.get("old_cvlan")):
                old_vlan = f"{old_service.get('old_svlan', '')} | {old_service.get('old_cvlan', '')}"
            title = (
                f"Ooredoo - Relocation - {address} / Account #: {account} / "
                f"{bandwidth}/ Ticket ID:{ticket_id}"
            )
            body = "\n".join(
                [
                    "Ooredoo - Relocation",
                    f"Ticket ID : {ticket_id}",
                    f"Ticket URL: {ticket_url}",
                    f"Account # : {account}",
                    f"Bandwidth : {bandwidth}",
                    f"Customer Name: {name}",
                    f"Phone : {phone}",
                    "",
                    f"New Address: {address}",
                    f"HDC ONT FSAN (New):{(' ' + fsan) if fsan else ''}",
                    "",
                    f"Old Address: {old_value(old_service, 'old_address', 'Old Address')}",
                    f"Old SVLAN | CVLAN : {old_vlan or '<enter Old SVLAN | CVLAN>'}",
                    f"HDC ONT FSAN (Old): {old_value(old_service, 'old_fsan', 'HDC ONT FSAN (Old)')}",
                ]
            )
            return f"{title}\n\n{body}"

        service = self._service_label(text)  # e.g. "New Service"

        title = (
            f"Ooredoo - {service} - {address} / Account #: {account} / "
            f"{bandwidth}/ Ticket ID:{ticket_id}"
        )

        body = "\n".join(
            [
                f"Ooredoo - {service}",
                f"Ticket ID : {ticket_id}",
                f"Ticket URL: {ticket_url}",
                f"Bandwidth : {bandwidth}",
                f"Account # : {account}",
                f"Address: {address}",
                f"Customer Name: {name}",
                f"Phone : {phone}",
                f"HDC ONT FSAN:{(' ' + fsan) if fsan else ''}",
                "Other info:",
            ]
        )

        return f"{title}\n\n{body}"


# --------------------------------------------------------------------------- #
# Dhiraagu (AFAS)
# --------------------------------------------------------------------------- #
class DhiraaguFormatter(BaseFormatter):
    name = "Dhiraagu"
    detect_keywords = ("dhiraagu", "afas", "svlan", "cvlan")
    manual_fields = (("order_url_id", "Order URL ID"),)

    ORDER_URL = "https://afas.dhiraagu.com.mv/orders/hdc/{order_url_id}?activeRelationManager=notes"

    # Order type -> service label used in the heading. "relocation" maps here
    # too: reaching this (non-relocation) branch with a portal Order type of
    # "Relocation" only happens when NocBot found no existing service for the
    # account, so this ticket is being deliberately treated as New Service --
    # the heading should say that, not parrot the portal's stale label.
    SERVICE_LABELS = {
        "new service": "New Service",
        "relocation": "New Service",
    }

    def _service_label(self, text: str) -> str:
        otype = field_after_label(text, "Order type") or ""
        return self.SERVICE_LABELS.get(otype.strip().lower(), otype.strip() or "New Service")

    def _is_tv(self, text: str) -> bool:
        """DhiraaguTV orders use a slightly different block (see _tv_address /
        format). Detected when the 'Service profile' field starts with 'DhiraaguTV'."""
        profile = field_after_label(text, "Service profile") or ""
        return profile.strip().lower().startswith("dhiraagutv")

    def _tv_profile(self, text: str) -> str:
        """The TV service profile without its bandwidth suffix:
        'DhiraaguTV (30|30M)' -> 'DhiraaguTV'."""
        profile = field_after_label(text, "Service profile") or ""
        return re.sub(r"\s*\(.*\)\s*$", "", profile).strip()

    def _tv_address(self, text: str) -> str:
        """TV address: split the trailing letter off the building, then glue
        floor+apartment together. Building 'V3A' -> 'V3-A', Floor '3' + Apt '06'
        -> '306' => 'V3-A-306'."""
        building = (field_after_label(text, "Building/Tower") or "").strip()
        floor = (field_after_label(text, "Floor") or "").strip()
        apartment = (field_after_label(text, "Apartment") or "").strip()
        m = re.match(r"^(.*?)([A-Za-z]+)$", building)
        if m and m.group(1):
            building = f"{m.group(1)}-{m.group(2)}"
        return build_address(building, f"{floor}{apartment}")

    def address_id_for_validation(self, text: str) -> Optional[str]:
        if self._is_tv(text):
            return self._tv_address(text) or None
        return build_address(
            field_after_label(text, "Building/Tower"),
            field_after_label(text, "Floor"),
            field_after_label(text, "Apartment"),
        ) or None

    def phone_for_display(self, text: str) -> Optional[str]:
        return local_phone(field_after_label(text, "Contact number")) or None

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        manual = manual or {}
        order_no = field_after_label(text, "Order number") or ""
        service_no = field_after_label(text, "Service number") or ""
        name = dedupe_name(field_after_label(text, "Customer name"))
        phone = local_phone(field_after_label(text, "Contact number"))
        package = field_after_label(text, "Service package") or ""
        svlan = field_after_label(text, "Svlan") or ""
        cvlan = field_after_label(text, "Cvlan") or ""
        fsan = field_after_label(text, "HDC ONT FSAN") or ""

        address = build_address(
            field_after_label(text, "Building/Tower"),
            field_after_label(text, "Floor"),
            field_after_label(text, "Apartment"),
        )

        url_id = manual_value(manual, "order_url_id", "Order URL ID")
        order_url = self.ORDER_URL.format(order_url_id=url_id)

        if relocation:
            old_vlan = ""
            if old_service and (old_service.get("old_svlan") or old_service.get("old_cvlan")):
                old_vlan = f"{old_service.get('old_svlan', '')} / {old_service.get('old_cvlan', '')}"
            title = (
                f"Dhiraagu - Relocation - {address} / Service #: {service_no}/ "
                f"{package}/ Order ID:{order_no}"
            )
            body = "\n".join(
                [
                    "Dhiraagu - Relocation",
                    f"Order ID : {order_no}",
                    f"Order URL: {order_url}",
                    f"Service # : {service_no}",
                    f"Service Profile : {package}",
                    f"Customer Name: {name}",
                    f"Phone : {phone}",
                    "",
                    f"New Address: {address}",
                    f"New SVLAN / CVLAN: {svlan} / {cvlan}",
                    "HDC ONT FSAN (New): <enter HDC ONT FSAN (New)>",
                    "",
                    f"Old Address: {old_value(old_service, 'old_address', 'Old Address')}",
                    f"Previous SVLAN / CVLAN: {old_vlan or '<enter Previous SVLAN / CVLAN>'}",
                    f"ONT FSAN (Old): {old_value(old_service, 'old_fsan', 'ONT FSAN (Old)')}",
                ]
            )
            return f"{title}\n\n{body}"

        service = self._service_label(text)  # e.g. "New Service"

        if self._is_tv(text):
            tv_address = self._tv_address(text)
            tv_profile = self._tv_profile(text)
            title = (
                f"Dhiraagu - {service} - {tv_address} / Service #: {service_no}/ "
                f"{tv_profile}/ Order ID:{order_no}"
            )
            body = "\n".join(
                [
                    f"Dhiraagu - {service}",
                    f"Order ID : {order_no}",
                    f"Order URL: {order_url}",
                    f"Service # : {service_no}",
                    f"Address: {tv_address}",
                    f"Customer Name: {name}",
                    f"Phone : {phone}",
                    f"Service Profile : {tv_profile}",
                    f"SVLAN | CVLAN: {svlan} | {cvlan}",
                    "ONT FSAN:",
                ]
            )
            return f"{title}\n\n{body}"

        title = (
            f"Dhiraagu - {service} - {address} / Service #: {service_no}/ "
            f"{package}/ Order ID:{order_no}"
        )

        body = "\n".join(
            [
                f"Dhiraagu - {service}",
                f"Order ID : {order_no}",
                f"Order URL: {order_url}",
                f"Service # : {service_no}",
                f"Address: {address}",
                f"Customer Name: {name}",
                f"Phone : {phone}",
                f"Service Profile : {package}",
                f"SVLAN | CVLAN: {svlan} | {cvlan}",
                f"HDC ONT FSAN:{(' ' + fsan) if fsan else ''}",
            ]
        )

        return f"{title}\n\n{body}"


# --------------------------------------------------------------------------- #
# Medianet (CRM.COM)
# --------------------------------------------------------------------------- #
class MedianetFormatter(BaseFormatter):
    name = "Medianet"
    detect_keywords = ("medianet", "media net", "crm.com")

    SERVICE_LABEL = "New Service"

    def _contact(self, text: str) -> tuple[str, str, str, str]:
        """Parse the Contact Details block -> (name, account, phone_raw, address_raw).

        The block is "Name (account)" then phone, then address — but some tickets
        slip an email line in between, so identify each line by shape (skip emails,
        pick the phone, take the first address-like line) rather than by position.
        """
        block = lines_after_label(text, "Contact Details", 6)
        name_account = block[0] if block else ""
        phone_raw = ""
        address_raw = ""
        for line in block[1:]:
            if looks_like_email(line):
                continue
            if not phone_raw and looks_like_phone(line):
                phone_raw = line
            elif not address_raw and "," in line:  # addresses are comma-separated
                address_raw = line
        if not address_raw:
            address_raw = field_after_label(text, "Where") or ""
        name, account = split_name_account(name_account)
        return name, account, phone_raw, address_raw

    def address_id_for_validation(self, text: str) -> Optional[str]:
        return clean_building_code(self._contact(text)[3]) or None

    def account_id_for_validation(self, text: str) -> Optional[str]:
        return self._contact(text)[1] or None

    def phone_for_display(self, text: str) -> Optional[str]:
        return local_phone(self._contact(text)[2]) or None

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        ticket = field_after_label(text, "Service Request") or ""

        name, account, phone_raw, address_raw = self._contact(text)
        phone = local_phone(phone_raw)
        address = clean_building_code(address_raw)

        if relocation:
            title = (
                f"Medianet - Relocation - {address} / Account #: {account}/ Ticket #:{ticket}"
            )
            body = "\n".join(
                [
                    "Medianet - Relocation",
                    f"Ticket ID : {ticket}",
                    f"Account # : {account}",
                    f"Customer Name: {name}",
                    f"Phone : {phone}",
                    "",
                    f"New Address: {address}",
                    "HDC ONT FSAN (New): <enter HDC ONT FSAN (New)>",
                    "",
                    f"Old Address: {old_value(old_service, 'old_address', 'Old Address')}",
                    f"HDC ONT FSAN (Old): {old_value(old_service, 'old_fsan', 'HDC ONT FSAN (Old)')}",
                    "",
                    "ONT Contract Status: Signed",
                ]
            )
            return f"{title}\n\n{body}"

        service = self.SERVICE_LABEL

        title = (
            f"Medianet - {service} - {address} / Account #: {account}/ Ticket #:{ticket}"
        )

        body = "\n".join(
            [
                f"Medianet - {service}",
                f"Ticket # : {ticket}",
                f"Account # : {account}",
                f"Address: {address}",
                f"Customer Name: {name}",
                f"Phone : {phone}",
                "HDC ONT FSAN:",
            ]
        )

        return f"{title}\n\n{body}"


# --------------------------------------------------------------------------- #
# Raajje Online (ROL) — Kayako helpdesk
# --------------------------------------------------------------------------- #
class RolFormatter(BaseFormatter):
    name = "Raajje Online (ROL)"
    detect_keywords = ("raajje online", "raajjeonline", "support.rol.net.mv", "rol.mv", "rol")

    TICKET_URL = "https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{internal_id}/inbox/55/-1/-1"

    def _display_id(self, text: str) -> str:
        """The ROL reference shown on the ticket, e.g. ROL260195."""
        m = re.search(r"\bROL\d+\b", text, re.IGNORECASE)
        return m.group(0).upper() if m else ""

    def _internal_id(self, text: str) -> str:
        """The Kayako TICKET ID (used to build the ticket URL), e.g. 115427."""
        return (field_after_label(text, "TICKET ID") or "").strip()

    def _service_label(self, text: str) -> str:
        return (field_after_label(text, "TYPE") or "").strip() or "New Connection"

    def _posted_fields(self, text: str) -> tuple[str, str, str, str, str]:
        """Parse the original customer post block (after 'Posted on:', up to the
        footer) into (name, phone, building, area, package). The ROL reference line
        is dropped; the remaining lines are, in order: name, phone, building code,
        [area], package (last)."""
        lines = _lines(text)
        start = next((i + 1 for i, ln in enumerate(lines)
                      if ln.lower().startswith("posted on:")), None)
        if start is None:
            return "", "", "", "", ""
        block: list[str] = []
        for ln in lines[start:]:
            low = ln.lower()
            if low.startswith(("last edited by:", "ip address:", "email to:", "«", "page ")):
                break
            if ln and not re.fullmatch(r"ROL\d+", ln, re.IGNORECASE):
                block.append(ln)
        name = block[0] if len(block) > 0 else ""
        phone = block[1] if len(block) > 1 else ""
        building = block[2] if len(block) > 2 else ""
        package = block[-1] if len(block) > 3 else ""
        # The area (e.g. "Hulhumale Phase 2") sits between building and package when
        # the block has an extra line.
        area = block[3] if len(block) >= 5 else ""
        return name, phone, building, area, package

    def address_id_for_validation(self, text: str) -> Optional[str]:
        # Just the building code — the area is for display, not the Znuny CustomerID.
        return self._posted_fields(text)[2] or None

    def account_id_for_validation(self, text: str) -> Optional[str]:
        return self._display_id(text) or None

    def phone_for_display(self, text: str) -> Optional[str]:
        return local_phone(self._posted_fields(text)[1]) or None

    def display_address(self, text: str) -> str:
        """Building code with the area appended, e.g. 'UD-06-15-05, Hulhumale Phase 2'.
        Shared with the ROL extractor so the stored ticket.address matches this format."""
        _, _, building, area, _ = self._posted_fields(text)
        return f"{building}, {area}" if area else building

    def format(self, text: str, manual: dict | None = None, relocation: bool = False,
               old_service: dict | None = None) -> str:
        account = self._display_id(text)        # ROL###### reference -> Account #
        ticket_id = self._internal_id(text)     # Kayako TICKET ID -> Ticket ID + URL
        name_raw, phone_raw, building, area, bandwidth = self._posted_fields(text)
        name = dedupe_name(name_raw)
        phone = local_phone(phone_raw)
        # Display the building code with the area appended, e.g. "UD-06-15-05, Hulhumale Phase 2".
        address = f"{building}, {area}" if area else building
        ticket_url = self.TICKET_URL.format(internal_id=ticket_id) if ticket_id else ""

        if relocation:
            old_vlan = ""
            if old_service and (old_service.get("old_svlan") or old_service.get("old_cvlan")):
                old_vlan = f"{old_service.get('old_svlan', '')} | {old_service.get('old_cvlan', '')}"
            title = (
                f"ROL - Relocation - {address} / Account #: {account} / "
                f"{bandwidth}/ Ticket ID:{ticket_id}"
            )
            body = "\n".join(
                [
                    "ROL - Relocation",
                    f"Ticket ID : {ticket_id}",
                    f"Ticket URL : {ticket_url}",
                    f"Account # : {account}",
                    f"Bandwidth : {bandwidth}",
                    f"Customer Name: {name}",
                    f"Phone : {phone}",
                    "",
                    f"New Address: {address}",
                    "",
                    f"Old Address: {old_value(old_service, 'old_address', 'Old Address')}",
                    f"Old SVLAN | CVLAN: {old_vlan or '<enter Old SVLAN | CVLAN>'}",
                    f"HDC ONT FSAN (Old): {old_value(old_service, 'old_fsan', 'HDC ONT FSAN (Old)')}",
                    "",
                    "Other info:",
                    "ONT Contract Status: Signed / Not Signed",
                ]
            )
            return f"{title}\n\n{body}"

        service = self._service_label(text)
        title = (
            f"ROL - {service} - {address} / Account #: {account} / "
            f"{bandwidth}/ Ticket ID:{ticket_id}"
        )
        body = "\n".join(
            [
                f"ROL - {service}",
                f"Ticket ID : {ticket_id}",
                f"Ticket URL : {ticket_url}",
                f"Account # : {account}",
                f"Bandwidth : {bandwidth}",
                f"Customer Name: {name}",
                f"Phone : {phone}",
                f"Address: {address}",
                "",
                "Other info:",
            ]
        )
        return f"{title}\n\n{body}"


# Order matters only as a tie-break fallback; detection uses the best score.
ALL_FORMATTERS: list[BaseFormatter] = [
    OoredooFormatter(),
    DhiraaguFormatter(),
    MedianetFormatter(),
    RolFormatter(),
]


def detect_formatter(text: str) -> Optional[BaseFormatter]:
    """Return the best-matching formatter for the dump, or None."""
    best, best_score = None, 0
    for fmt in ALL_FORMATTERS:
        score = fmt.detect_score(text)
        if score > best_score:
            best, best_score = fmt, score
    return best
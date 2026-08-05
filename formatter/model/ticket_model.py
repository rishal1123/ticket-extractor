"""Application model: turns raw pasted text + manual values into a result.

This is the MVC "model" that the controller talks to. It wraps the domain
formatters (``formatters.py``) and returns a structured :class:`FormatResult`
the view can render without knowing anything about ISP parsing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .formatters import detect_formatter, validate_address, validate_value

logger = logging.getLogger(__name__)


@dataclass
class FormatResult:
    """Outcome of formatting one pasted dump."""

    isp: Optional[str]
    text: str
    manual_fields: tuple[tuple[str, str], ...] = ()
    missing_keys: tuple[str, ...] = ()
    # Non-blocking validation warnings (e.g. an address that doesn't match the
    # expected format for its zone) and the values to highlight in the output.
    warnings: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()
    # True when the account already exists in Znuny — the ticket can be reformatted
    # as a Relocation. ``relocation`` is True when it currently IS so formatted.
    relocation_possible: bool = False
    relocation: bool = False

    @property
    def detected(self) -> bool:
        return self.isp is not None

    @property
    def complete(self) -> bool:
        """True when an ISP was detected and no manual fields are still empty.

        Warnings don't affect completeness — they inform but never block copy.
        """
        return self.detected and not self.missing_keys

    def missing_labels(self) -> list[str]:
        labels = dict(self.manual_fields)
        return [labels[k] for k in self.missing_keys]


class TicketModel:
    """Detect the ISP and format a pasted dump into a standardized ticket block."""

    def build(
        self,
        raw: str,
        manual: Optional[dict] = None,
        address_rules: Optional[dict] = None,
        znuny=None,
        relocation: Optional[bool] = None,
        ticket_type: Optional[str] = None,
        nocbot=None,
    ) -> FormatResult:
        """``relocation``: None auto-decides (see below); True/False forces the
        New Service/Relocation template regardless of ``ticket_type``, e.g. the
        formatter page's manual "Convert to Relocation" / "New Service" toggle.
        """
        raw = (raw or "").strip()
        manual = manual or {}

        if not raw:
            return FormatResult(isp=None, text="")

        fmt = detect_formatter(raw)
        if fmt is None:
            logger.debug("No ISP matched the pasted data (%d chars)", len(raw))
            return FormatResult(isp=None, text="Could not detect the ISP from the pasted data.")

        # Auto-decide Relocation vs New Service for a portal-reported
        # relocation ticket when the caller hasn't explicitly forced one or
        # the other: a genuine relocation only makes sense if the account
        # already has a provisioned service to relocate FROM, so check NocBot
        # and fall back to New Service when it doesn't -- the portal's own
        # "Relocation" label isn't always reliable (e.g. applied to what's
        # actually a fresh install). When the caller DID force relocation
        # (e.g. the manual toggle), still try to auto-fill the old-service
        # fields from NocBot if available, purely as a convenience.
        old_service: Optional[dict] = None
        is_relocation = relocation
        if is_relocation is None:
            is_relocation = False
            if ticket_type and ticket_type.strip().lower() == "relocation" and nocbot is not None:
                account_id = (fmt.account_id_for_validation(raw) or "").strip()
                if account_id:
                    old_service = nocbot.lookup_relocation_data(account_id)
                    is_relocation = old_service is not None
        elif is_relocation and nocbot is not None:
            account_id = (fmt.account_id_for_validation(raw) or "").strip()
            if account_id:
                old_service = nocbot.lookup_relocation_data(account_id)

        # Fault tickets get their own template (see formatters.py) -- driven by
        # the portal's own ticket_type, the same reliable source relocation
        # auto-detection uses, rather than re-parsing a "Ticket Type" field out
        # of the dump per-ISP (Medianet's dump doesn't even expose one cleanly).
        is_fault = bool(ticket_type and ticket_type.strip().lower() == "fault")

        logger.debug("Detected ISP: %s (relocation=%s, fault=%s)", fmt.name, is_relocation, is_fault)
        text = fmt.format(raw, manual, relocation=is_relocation, old_service=old_service, is_fault=is_fault)
        missing = tuple(
            key for key, _ in fmt.manual_fields if not manual.get(key, "").strip()
        )

        # Validate fields against the (admin-configured) rules for this ISP.
        # Rules shape per ISP: {"atoll": [...], "zones": {zone: [patterns]}}.
        warnings: list[str] = []
        flagged: list[str] = []
        relocation_possible = False
        isp_rules = (address_rules or {}).get(fmt.name) or {}

        # Atoll check (e.g. Ooredoo Atoll must be "Kaafu").
        atoll_allowed = isp_rules.get("atoll") or []
        if atoll_allowed:
            atoll_val = fmt.atoll_for_validation(raw)
            if atoll_val is not None:
                msg = validate_value("Atoll", atoll_val, atoll_allowed)
                if msg:
                    logger.info("Atoll validation failed for %s: %s", fmt.name, msg)
                    warnings.append(msg)
                    flagged.append(atoll_val.strip())

        # Address check, keyed by zone (e.g. Ooredoo street -> address ID format).
        zones = isp_rules.get("zones") or {}
        components = fmt.address_for_validation(raw)
        if components and zones:
            zone, address_id = components
            msg = validate_address(zone, address_id, zones)
            if msg:
                logger.info("Address validation failed for %s: %s", fmt.name, msg)
                warnings.append(msg)
                flagged.append(address_id)

        # Cross-check against Znuny (network IO is injected via the znuny service
        # to keep this layer pure). Two checks:
        #   * the address must exist as a ticket CustomerID (exact), else warn;
        #   * if the account already has a ticket, flag a possible relocation.
        if znuny is not None:
            address_id = (fmt.address_id_for_validation(raw) or "").strip()
            if address_id:
                result = znuny.verify_address(address_id)
                msg = None
                if result is False:
                    msg = (f'Address "{address_id}" not found in Znuny '
                           f'(no ticket with this CustomerID)')
                elif result is None:
                    msg = f'Address "{address_id}" could not be verified in Znuny (unreachable)'
                if msg:
                    logger.info("Znuny address check for %s: %s", fmt.name, msg)
                    warnings.append(msg)
                    if address_id not in flagged:
                        flagged.append(address_id)

            account_id = (fmt.account_id_for_validation(raw) or "").strip()
            if account_id and znuny.account_exists(account_id):
                relocation_possible = True
                msg = f"Possible relocation: account {account_id} already exists in Znuny"
                logger.info("Znuny account check for %s: %s", fmt.name, msg)
                warnings.append(msg)
                if account_id not in flagged:
                    flagged.append(account_id)

        return FormatResult(
            isp=fmt.name,
            text=text,
            manual_fields=fmt.manual_fields,
            missing_keys=missing,
            warnings=tuple(warnings),
            flagged=tuple(flagged),
            relocation_possible=relocation_possible,
            relocation=is_relocation,
        )
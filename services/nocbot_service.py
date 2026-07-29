"""
NocBot Service - Checks whether an ONT exists in SMX for a ticket's address,
via NocBot's authenticated external API (see API_GUIDE.md: GET /api/ont/<ont_id>).

The ISP ticket's `address` field is sent as the ont_id. Fail-soft throughout:
network/auth/format problems never raise into the scheduler, they just leave
the ticket's ont_exists unresolved for the next retry.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional
from urllib.parse import quote

import httpx

from config import Config
from database import Database, now_maldives
from utils.logger import get_logger

logger = get_logger("nocbot_service")

REQUEST_TIMEOUT = 10.0
RETRY_AFTER_HOURS = 1
BATCH_SIZE = 20


class NocBotService:
    """Client for NocBot's ONT lookup API + the scheduler-facing check step."""

    def __init__(self, db: Database = None):
        self.db = db or Database()
        self.base_url = (Config.get_nocbot_url() or "").rstrip("/")
        self.api_key = Config.get_nocbot_api_key()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    def check_ont(self, address: str) -> Optional[bool]:
        """Look up an ONT by address (sent as NocBot's ont_id). Returns True if
        found, False if confirmed not found (404), None if the check was
        inconclusive (network error, rate limited, bad ont_id format, etc.)."""
        if not self.enabled or not address:
            return None

        url = f"{self.base_url}/api/ont/{quote(address, safe='')}"
        try:
            resp = httpx.get(url, headers={"X-API-Key": self.api_key}, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            logger.warning(f"NocBot ONT check failed for address '{address}': {e}")
            return None

        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        if resp.status_code == 400:
            logger.info(f"NocBot rejected ont_id format for address '{address}'")
            return None
        if resp.status_code == 401:
            logger.error("NocBot API key rejected (401) - check Admin > Config")
            return None
        if resp.status_code == 429:
            logger.info("NocBot rate limit hit - will retry next cycle")
            return None
        logger.warning(f"NocBot ONT check for '{address}' returned unexpected status {resp.status_code}")
        return None

    def _check_with_fallback(self, ticket_id: int, address: str, in_znuny: bool,
                              znuny_address: Optional[str]) -> Optional[bool]:
        """Check one ticket's ONT status (portal address, then the Znuny address
        as a fallback), persist it, and return the result. Shared by the
        scheduled batch check and the on-demand single-ticket check so both
        apply the exact same logic.

        For a ticket linked to Znuny, a portal-address check that didn't come
        back found gets a second try against the Znuny phone-ticket address
        (znuny_address) before giving up. This covers both a confirmed 404 *and*
        an inconclusive result (e.g. NocBot's ont_id format rejects commas/
        spaces, which a fair number of portal addresses contain) —
        znuny_address is a separately-captured address that's often cleaner/
        more accurate than the portal's, so it's worth a shot in either case."""
        result = self.check_ont(address)

        if result is not True and in_znuny and znuny_address and znuny_address != address:
            fallback = self.check_ont(znuny_address)
            if fallback is True:
                logger.info(
                    f"NocBot ONT found via Znuny address for ticket {ticket_id} "
                    f"('{znuny_address}' vs portal '{address}')"
                )
                result = True
            elif fallback is False:
                # A confirmed not-found is stronger signal than an inconclusive
                # portal-address result (e.g. a 400 tells us nothing either way).
                result = False

        self.db.update_ont_status(ticket_id, result)
        return result

    def check_pending_tickets(self, limit: int = BATCH_SIZE) -> dict:
        """Check active ISP tickets whose ONT existence is unknown, or was 'not
        found' more than RETRY_AFTER_HOURS ago. Persists the result per ticket."""
        if not self.enabled:
            return {"checked": 0, "found": 0}

        cutoff = now_maldives() - timedelta(hours=RETRY_AFTER_HOURS)
        candidates = self.db.get_tickets_needing_ont_check(cutoff=cutoff, limit=limit)

        checked = 0
        found = 0
        for t in candidates:
            result = self._check_with_fallback(t["id"], t["address"], bool(t.get("in_znuny")), t.get("znuny_address"))
            checked += 1
            if result:
                found += 1

        if checked:
            logger.info(f"NocBot ONT check: {checked} checked, {found} found")
        return {"checked": checked, "found": found}

    def check_ticket(self, ticket) -> Optional[bool]:
        """Check ONT status for one ticket right now, on demand -- e.g. when
        staff click "Check Znuny" in the ticket detail modal, rather than
        waiting for the next scheduled batch. Skipped (returns None without
        an API call) if the ticket has no address or NocBot isn't configured."""
        if not self.enabled or not getattr(ticket, "address", None):
            return None
        return self._check_with_fallback(
            ticket.id, ticket.address, bool(ticket.in_znuny), getattr(ticket, "znuny_address", None)
        )

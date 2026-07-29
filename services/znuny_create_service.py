"""
Znuny Create Service - Creates a new ISP ticket in Znuny via the Generic
Interface REST API (GenericTicketConnectorREST / TicketCreate), mirroring
what staff do by hand through "New phone ticket" in the Znuny agent UI.

Uses a deliberately separate, admin-configured credential set
(Config.get_znuny_create_*) from the read-only sync client in
znuny_client.py -- creating tickets is a write, consequential action, so it's
never assumed available just because read credentials are configured.

Field mapping was verified against a live Znuny TicketCreate call (queue,
type, customer user/ID, DynamicField_ISPTicket, article) before this was
written -- see the AgentTicketPhone ("New phone ticket") form for the
human-facing equivalent of every field set here.
"""

from __future__ import annotations

import httpx

from config import Config
from utils.logger import get_logger
from utils.ticket_formatter import format_ticket_dump, manual_from_ticket

logger = get_logger("znuny_create_service")

WEBSERVICE_PATH = "/otrs/nph-genericinterface.pl/Webservice/GenericTicketConnectorREST"
REQUEST_TIMEOUT = 15.0

# Portal -> destination queue (matches the queues already seen in synced Znuny data).
PORTAL_QUEUE = {
    "dhiraagu": "*Dhiraagu",
    "ooredoo": "*Ooredoo",
    "medianet": "*Medianet",
    "rol": "*ROL",
}

# States available on the "New phone ticket" form (Action=AgentTicketPhone ->
# NextStateID), read from a live Znuny instance. TicketCreate takes the state
# by name, not ID, so this is just the list of valid name strings -- no numeric
# IDs needed. "Open" first as the sensible default.
DEFAULT_STATE = "Open"
ZNUNY_STATES = [
    "Open",
    "1. Call Customer",
    "2. SD Team Site",
    "3. Field Team Site",
    "4. Ready for Provision",
    "5. ISP Issue",
    "5. ONT Lost",
    "6. ONT Contract Pending",
    "7. Building Contractor Pending",
    "8. HDC Contractor Pending",
    "9. Customer Pending",
    "Dhiraagu - Suspended",
    "*FDC - Contractor Pending",
    "*FDC - Contractor Resolved",
    "*FDC - Customer Renovation",
    "*FDC - ONT Power",
    "*FDC - Site Pending",
    "*FDC - Site Retry",
    "*FDC - Tower Hold",
]


class ZnunyCreateService:
    """Creates ISP tickets in Znuny. Fails soft/returns a clear error dict --
    never raises into the caller for an expected failure (missing config,
    Znuny validation error, customer user not found, etc.)."""

    def __init__(self):
        self.base_url = (Config.get_znuny_create_url() or "").rstrip("/")
        self.username = Config.get_znuny_create_username()
        self.password = Config.get_znuny_create_password()
        self.ws_url = f"{self.base_url}{WEBSERVICE_PATH}" if self.base_url else ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.username and self.password)

    def create_isp_ticket(self, ticket, relocation: bool = False, state: str = DEFAULT_STATE) -> dict:
        """Create a Znuny ticket for an ISP portal ticket that isn't linked yet.

        Returns {success, znuny_ticket_id, znuny_url, error}. znuny_ticket_id
        is the human ticket number (e.g. "2026072528000493"), matching the
        tickets.znuny_ticket_id column convention used everywhere else."""
        if not self.enabled:
            return self._fail("Znuny ticket creation isn't configured (Admin > Config).")

        if state not in ZNUNY_STATES:
            state = DEFAULT_STATE

        queue = PORTAL_QUEUE.get((ticket.portal or "").lower())
        if not queue:
            return self._fail(f"No Znuny queue mapping for portal '{ticket.portal}'.")

        account = (ticket.account or "").strip()
        if not account:
            return self._fail("Ticket has no account number to use as the Znuny customer user.")

        formatted = format_ticket_dump(
            ticket.raw_dump, ticket.portal, manual_from_ticket(ticket), relocation=relocation
        )
        if not formatted.get("ok"):
            return self._fail(formatted.get("error") or "Could not format this ticket.")
        if formatted.get("missing"):
            return self._fail(f"Fill in before creating: {', '.join(formatted['missing'])}")

        title, _, body = formatted["text"].partition("\n\n")
        title = title.strip() or f"{ticket.portal} - {ticket.ticket_id}"
        body = body.strip() or formatted["text"]

        customer_id = (ticket.znuny_address or ticket.address or "").strip() or None

        payload = {
            "UserLogin": self.username,
            "Password": self.password,
            "Ticket": {
                "Title": title,
                "Queue": queue,
                "Type": "Relocation" if relocation else "New Service",
                "State": state,
                "Priority": "Regular",
                "CustomerUser": account,
                **({"CustomerID": customer_id} if customer_id else {}),
            },
            "Article": {
                "Subject": title,
                "Body": body,
                "MimeType": "text/plain",
                "Charset": "utf8",
                "CommunicationChannel": "Phone",
            },
            "DynamicField": [
                {"Name": "ISPTicket", "Value": str(ticket.ticket_id)},
            ],
        }

        try:
            resp = httpx.post(f"{self.ws_url}/Ticket", json=payload, timeout=REQUEST_TIMEOUT, verify=False)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Znuny TicketCreate request failed for ticket {ticket.id}: {e}")
            return self._fail(f"Request to Znuny failed: {e}")

        if isinstance(data, dict) and data.get("Error"):
            err = data["Error"]
            logger.warning(f"Znuny TicketCreate rejected ticket {ticket.id}: {err}")
            return self._fail(err.get("ErrorMessage") or str(err))

        ticket_number = data.get("TicketNumber")
        ticket_id_internal = data.get("TicketID")
        if not ticket_number or not ticket_id_internal:
            logger.error(f"Znuny TicketCreate returned an unexpected shape: {data}")
            return self._fail("Znuny accepted the request but didn't return a ticket number.")

        znuny_url = self._ticket_url(ticket_id_internal)
        logger.info(f"Created Znuny ticket {ticket_number} for ISP ticket {ticket.id} ({ticket.portal}/{ticket.ticket_id})")
        return {"success": True, "znuny_ticket_id": ticket_number, "znuny_url": znuny_url, "error": None}

    def _ticket_url(self, internal_ticket_id) -> str:
        return f"{self.base_url}/otrs/index.pl?Action=AgentTicketZoom;TicketID={internal_ticket_id}"

    @staticmethod
    def _fail(error: str) -> dict:
        return {"success": False, "znuny_ticket_id": None, "znuny_url": None, "error": error}

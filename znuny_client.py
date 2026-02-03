import httpx
from typing import Optional
from config import Config
from utils.logger import get_logger

logger = get_logger("znuny")


class ZnunyClient:
    """Client for interacting with Znuny REST API."""

    def __init__(self):
        self.base_url = Config.ZNUNY_URL.rstrip("/")
        self.username = Config.ZNUNY_USERNAME
        self.password = Config.ZNUNY_PASSWORD
        self.session_id: Optional[str] = None

    async def _get_session(self) -> str | None:
        """Get or create a session with Znuny."""
        if self.session_id:
            return self.session_id

        try:
            async with httpx.AsyncClient() as client:
                # Znuny uses different API endpoints depending on version
                # Common endpoint for session creation
                response = await client.post(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Session",
                    json={
                        "UserLogin": self.username,
                        "Password": self.password
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    self.session_id = data.get("SessionID")
                    logger.info("Znuny session created successfully")
                    return self.session_id
                else:
                    logger.error(f"Failed to create Znuny session: {response.status_code}")
                    return None

        except Exception as e:
            logger.error(f"Error creating Znuny session: {e}")
            return None

    async def search_ticket(self, ticket_number: str) -> dict | None:
        """
        Search for a ticket in Znuny by ticket number.
        Returns ticket data if found, None otherwise.
        """
        session_id = await self._get_session()
        if not session_id:
            return None

        try:
            async with httpx.AsyncClient() as client:
                # Search for ticket by ticket number
                response = await client.get(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Ticket",
                    params={
                        "SessionID": session_id,
                        "TicketNumber": ticket_number
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    tickets = data.get("Ticket", [])
                    if tickets:
                        return tickets[0] if isinstance(tickets, list) else tickets
                    return None
                else:
                    logger.warning(f"Ticket search returned {response.status_code}")
                    return None

        except Exception as e:
            logger.error(f"Error searching ticket in Znuny: {e}")
            return None

    async def ticket_exists(self, ticket_number: str) -> bool:
        """Check if a ticket exists in Znuny."""
        result = await self.search_ticket(ticket_number)
        return result is not None

    async def search_by_title(self, title: str) -> list[dict]:
        """
        Search for tickets by title/subject.
        Useful for matching portal tickets to Znuny tickets.
        """
        session_id = await self._get_session()
        if not session_id:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Ticket",
                    params={
                        "SessionID": session_id,
                        "Title": f"*{title}*"
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("Ticket", [])
                return []

        except Exception as e:
            logger.error(f"Error searching tickets by title: {e}")
            return []

    async def search_by_customer(self, customer_name: str) -> list[dict]:
        """Search for tickets by customer name."""
        session_id = await self._get_session()
        if not session_id:
            return []

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Ticket",
                    params={
                        "SessionID": session_id,
                        "CustomerUserLogin": f"*{customer_name}*"
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("Ticket", [])
                return []

        except Exception as e:
            logger.error(f"Error searching tickets by customer: {e}")
            return []

    async def check_ticket_in_znuny(self, portal_ticket_id: str, customer_name: str = None) -> tuple[bool, str | None]:
        """
        Check if a portal ticket exists in Znuny.
        Tries multiple search strategies.
        Returns: (exists: bool, znuny_ticket_id: str | None)
        """
        # Strategy 1: Search by portal ticket ID in title
        tickets = await self.search_by_title(portal_ticket_id)
        if tickets:
            znuny_id = tickets[0].get("TicketNumber") or tickets[0].get("TicketID")
            return True, str(znuny_id)

        # Strategy 2: Search by customer name if provided
        if customer_name:
            tickets = await self.search_by_customer(customer_name)
            # Look for recent tickets that might match
            for ticket in tickets:
                title = ticket.get("Title", "")
                if portal_ticket_id in title:
                    znuny_id = ticket.get("TicketNumber") or ticket.get("TicketID")
                    return True, str(znuny_id)

        return False, None

    async def close_session(self):
        """Close the Znuny session."""
        self.session_id = None


# Synchronous wrapper for use in non-async contexts
class ZnunyClientSync:
    """Synchronous wrapper for ZnunyClient."""

    def __init__(self):
        self.base_url = Config.ZNUNY_URL.rstrip("/")
        self.username = Config.ZNUNY_USERNAME
        self.password = Config.ZNUNY_PASSWORD
        self.session_id: Optional[str] = None

    def _get_session(self) -> str | None:
        if self.session_id:
            return self.session_id

        try:
            with httpx.Client() as client:
                response = client.post(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Session",
                    json={
                        "UserLogin": self.username,
                        "Password": self.password
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    self.session_id = data.get("SessionID")
                    return self.session_id
                return None

        except Exception as e:
            logger.error(f"Error creating Znuny session: {e}")
            return None

    def check_ticket_in_znuny(self, portal_ticket_id: str, customer_name: str = None) -> tuple[bool, str | None]:
        session_id = self._get_session()
        if not session_id:
            return False, None

        try:
            with httpx.Client() as client:
                # Search by title containing portal ticket ID
                response = client.get(
                    f"{self.base_url}/otrs/nph-genericinterface.pl/Webservice/REST/Ticket",
                    params={
                        "SessionID": session_id,
                        "Title": f"*{portal_ticket_id}*"
                    },
                    timeout=30.0
                )

                if response.status_code == 200:
                    data = response.json()
                    tickets = data.get("Ticket", [])
                    if tickets:
                        ticket = tickets[0] if isinstance(tickets, list) else tickets
                        znuny_id = ticket.get("TicketNumber") or ticket.get("TicketID")
                        return True, str(znuny_id)

                return False, None

        except Exception as e:
            logger.error(f"Error checking ticket in Znuny: {e}")
            return False, None

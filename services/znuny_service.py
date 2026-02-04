"""
Znuny Service - Business logic for Znuny ticket synchronization.
"""

from typing import Dict, List, Optional
from database import Database, now_maldives
from znuny_client import ZnunyClient
from utils.logger import get_logger

logger = get_logger("znuny_service")


class ZnunyService:
    """Service class for Znuny ticket synchronization."""

    def __init__(self, db: Database = None):
        """Initialize service with optional database instance."""
        self.db = db or Database()
        self._znuny_client: Optional[ZnunyClient] = None

    @property
    def znuny_client(self) -> ZnunyClient:
        """Lazy-load Znuny client."""
        if self._znuny_client is None:
            self._znuny_client = ZnunyClient()
        return self._znuny_client

    def check_ticket_in_znuny(self, ticket_id: int) -> Dict:
        """
        Check if a ticket exists in Znuny and update its status.

        Args:
            ticket_id: Internal ticket database ID

        Returns:
            Dict with in_znuny, znuny_ticket_id, message
        """
        ticket = self.db.get_ticket_by_id(ticket_id)
        if not ticket:
            return {"success": False, "message": "Ticket not found"}

        # Use account for ROL tickets, ticket_id for others
        search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id

        try:
            exists, znuny_id = self.znuny_client.check_ticket_sync(
                search_term, ticket.customer_name
            )
            self.db.update_znuny_status(ticket_id, exists, znuny_id)

            return {
                "success": True,
                "in_znuny": exists,
                "znuny_ticket_id": znuny_id,
                "message": "Found in Znuny" if exists else "Not found in Znuny"
            }
        except Exception as e:
            logger.error(f"Error checking Znuny for ticket {ticket_id}: {e}")
            return {"success": False, "message": str(e)}

    def sync_ticket_details(self, ticket_id: int) -> Dict:
        """
        Fetch and sync detailed information from Znuny for a ticket.

        Args:
            ticket_id: Internal ticket database ID

        Returns:
            Dict with synced details or error
        """
        ticket = self.db.get_ticket_by_id(ticket_id)
        if not ticket:
            return {"success": False, "message": "Ticket not found"}

        if not ticket.znuny_ticket_id:
            return {"success": False, "message": "Ticket not linked to Znuny"}

        try:
            details = self.znuny_client.get_ticket_details(ticket.znuny_ticket_id)
            if not details:
                return {"success": False, "message": "Could not fetch Znuny details"}

            # Update ticket with Znuny details - use actual Znuny creation time
            self.db.update_znuny_details(
                ticket_id,
                znuny_created_at=details.created_at,
                znuny_created_by=details.created_by,
                znuny_address=details.address
            )

            # Store articles
            for article in details.articles:
                self.db.upsert_znuny_article(
                    ticket_id=ticket_id,
                    znuny_ticket_id=ticket.znuny_ticket_id,
                    article_number=article.article_number,
                    sender=article.sender,
                    via=article.via,
                    subject=article.subject,
                    created_at=article.created_at,
                    created_at_str=article.created_at_str,
                    created_by=article.created_by,
                    body=article.body
                )

            return {
                "success": True,
                "znuny_created_by": details.created_by,
                "znuny_address": details.address,
                "articles_count": len(details.articles),
                "message": "Synced successfully"
            }
        except Exception as e:
            logger.error(f"Error syncing Znuny details for ticket {ticket_id}: {e}")
            return {"success": False, "message": str(e)}

    def sync_unchecked_tickets(self) -> Dict:
        """
        Check Znuny status for all tickets that haven't been checked yet.

        Returns:
            Dict with sync statistics
        """
        # Get all active tickets and filter for those not in Znuny
        all_tickets = self.db.get_all_tickets(include_completed=False)
        tickets = [t for t in all_tickets if not t.in_znuny]

        results = {"checked": 0, "found": 0, "not_found": 0, "errors": 0}

        for ticket in tickets:
            search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id

            try:
                exists, znuny_id = self.znuny_client.check_ticket_sync(
                    search_term, ticket.customer_name
                )
                self.db.update_znuny_status(ticket.id, exists, znuny_id)
                results["checked"] += 1

                if exists:
                    results["found"] += 1
                    # Sync details for found tickets
                    details = self.znuny_client.get_ticket_details(znuny_id)
                    if details:
                        self.db.update_znuny_details(
                            ticket.id,
                            znuny_created_at=details.created_at,
                            znuny_created_by=details.created_by,
                            znuny_address=details.address
                        )
                        for article in details.articles:
                            self.db.upsert_znuny_article(
                                ticket_id=ticket.id,
                                znuny_ticket_id=znuny_id,
                                article_number=article.article_number,
                                sender=article.sender,
                                via=article.via,
                                subject=article.subject,
                                created_at=article.created_at,
                                created_at_str=article.created_at_str,
                                created_by=article.created_by,
                                body=article.body
                            )
                else:
                    results["not_found"] += 1
            except Exception as e:
                logger.error(f"Error checking ticket {ticket.id}: {e}")
                results["errors"] += 1

        logger.info(f"Znuny sync complete: {results}")
        return results

    def sync_all_znuny_details(self) -> Dict:
        """
        Sync Znuny details for all tickets that are in Znuny but missing details.

        Returns:
            Dict with sync statistics
        """
        # Get all tickets that are in Znuny but missing znuny_created_by
        all_tickets = self.db.get_all_tickets(include_completed=True)
        tickets = [t for t in all_tickets if t.in_znuny and t.znuny_ticket_id and not t.znuny_created_by]

        results = {"synced": 0, "errors": 0}

        for ticket in tickets:
            result = self.sync_ticket_details(ticket.id)
            if result["success"]:
                results["synced"] += 1
            else:
                results["errors"] += 1

        logger.info(f"Znuny details sync complete: {results}")
        return results

    def get_sync_status(self) -> Dict:
        """
        Get overall Znuny sync status.

        Returns:
            Dict with sync statistics
        """
        stats = self.db.get_stats()
        all_tickets = self.db.get_all_tickets(include_completed=False)

        in_znuny = sum(1 for t in all_tickets if t.in_znuny)
        with_details = sum(1 for t in all_tickets if t.in_znuny and t.znuny_created_by)

        return {
            "total_active": stats.get("total", 0),
            "in_znuny": in_znuny,
            "not_in_znuny": stats.get("not_in_znuny", 0),
            "with_details": with_details,
            "needing_sync": in_znuny - with_details
        }

    def get_ticket_articles(self, ticket_id: int) -> List[Dict]:
        """Get Znuny articles for a ticket."""
        return self.db.get_znuny_articles(ticket_id=ticket_id)

    def close(self):
        """Clean up resources."""
        if self._znuny_client:
            self._znuny_client.close()

"""
Znuny Service - Business logic for Znuny ticket synchronization.
"""

from typing import Dict, List, Optional
from database import Database, now_maldives
from znuny_client import ZnunyClient, parse_site_visit_article
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
                znuny_address=details.address,
                znuny_url=details.znuny_url
            )

            # Store articles and extract site visits
            site_visits_found = 0
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

                # Check for site visit articles
                site_visit = parse_site_visit_article(article, ticket.znuny_ticket_id)
                if site_visit:
                    self.db.upsert_site_visit(
                        znuny_ticket_id=site_visit.znuny_ticket_id,
                        article_id=site_visit.article_number,
                        site_type=site_visit.site_type,
                        service_provider=site_visit.service_provider,
                        scheduled_time=site_visit.scheduled_time,
                        assigned_to=site_visit.assigned_to,
                        visit_date=site_visit.visit_date,
                        article_created_at=site_visit.article_created_at,
                        ticket_id=ticket_id,
                        znuny_url=details.znuny_url
                    )
                    site_visits_found += 1
                    logger.info(f"Found site visit in ticket {ticket.znuny_ticket_id}: {site_visit.assigned_to} at {site_visit.scheduled_time}")

            # Check for follow-up articles that complete pending site visits
            site_visits_completed = self._complete_site_visits_by_followup(
                ticket.znuny_ticket_id, details.articles
            )

            return {
                "success": True,
                "znuny_created_by": details.created_by,
                "znuny_address": details.address,
                "articles_count": len(details.articles),
                "site_visits_found": site_visits_found,
                "site_visits_completed": site_visits_completed,
                "message": "Synced successfully"
            }
        except Exception as e:
            logger.error(f"Error syncing Znuny details for ticket {ticket_id}: {e}")
            return {"success": False, "message": str(e)}

    def sync_unchecked_tickets(self) -> Dict:
        """
        OPTIMIZED: Check Znuny status for all tickets that haven't been checked yet.

        This method ONLY checks if tickets exist in Znuny and sets the znuny_ticket_id.
        Full detail syncing (articles, site visits) is handled by sync_all_site_visits().

        Returns:
            Dict with sync statistics
        """
        # Get all active tickets and filter for those not in Znuny
        all_tickets = self.db.get_all_tickets(include_completed=False)
        tickets = [t for t in all_tickets if not t.in_znuny]

        results = {"checked": 0, "found": 0, "not_found": 0, "errors": 0}

        if not tickets:
            logger.info("No unchecked tickets to sync")
            return results

        logger.info(f"Checking {len(tickets)} tickets in Znuny")

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
                    logger.info(f"Found {ticket.portal}/{ticket.ticket_id} in Znuny as {znuny_id}")
                else:
                    results["not_found"] += 1
            except Exception as e:
                logger.error(f"Error checking ticket {ticket.id}: {e}")
                results["errors"] += 1

        logger.info(f"ISP ticket check complete: {results['found']}/{results['checked']} found in Znuny")
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

    def _complete_site_visits_by_followup(self, znuny_ticket_id: str, articles: list) -> int:
        """
        Check if any pending site visits should be completed based on follow-up articles.
        A site visit is completed if there's an article with a higher article_number.

        Returns the count of site visits completed.
        """
        completed_count = 0
        pending_visits = self.db.get_pending_site_visits_for_ticket(znuny_ticket_id)

        if not pending_visits:
            return 0

        # Sort articles by article_number ascending
        sorted_articles = sorted(articles, key=lambda a: a.article_number)

        for visit in pending_visits:
            visit_article_id = visit["article_id"]

            # Find the first article after the site visit article
            for article in sorted_articles:
                if article.article_number > visit_article_id and article.created_at:
                    # Found a follow-up article - complete the site visit
                    if self.db.complete_site_visit_by_followup(
                        znuny_ticket_id, visit_article_id, article.created_at
                    ):
                        completed_count += 1
                        logger.info(f"Site visit {visit_article_id} completed by follow-up article {article.article_number}")
                    break

        return completed_count

    def get_site_visits(self, date_from: str = None, date_to: str = None,
                        assigned_to: str = None, status: str = None,
                        limit: int = 100, offset: int = 0) -> Dict:
        """Get site visits with optional filters."""
        return self.db.get_site_visits(
            date_from=date_from, date_to=date_to,
            assigned_to=assigned_to, status=status,
            limit=limit, offset=offset
        )

    def get_site_visit_staff_stats(self, date_from: str = None, date_to: str = None) -> List:
        """Get site visit statistics by assigned staff."""
        return self.db.get_site_visit_staff_stats(date_from, date_to)

    def get_site_visit_by_date(self, date_from: str = None, date_to: str = None) -> List:
        """Get site visits aggregated by date."""
        return self.db.get_site_visit_by_date(date_from, date_to)

    def sync_all_site_visits(self, force_refresh: bool = False) -> Dict:
        """
        OPTIMIZED comprehensive site visit sync:
        1. Sync details for ISP tickets that have znuny_ticket_id but no details yet
        2. Get open tickets from Znuny (uses TTL cache unless force_refresh)
        3. Prioritize tickets that:
           - Have "site visit" in title (need site visit extraction)
           - Are linked to unsynced ISP tickets
           - Have pending site visits needing completion check
        4. Skip fully-synced tickets that don't need updates
        5. Extract site visits from articles with "OAN Site Visit Arranged"

        Returns sync statistics.
        """
        results = {
            "znuny_tickets_found": 0,
            "znuny_tickets_processed": 0,
            "znuny_tickets_skipped": 0,
            "site_visits_extracted": 0,
            "site_visits_linked": 0,
            "site_visits_completed": 0,
            "isp_tickets_synced": 0,
            "isp_details_synced": 0,
            "errors": 0
        }

        logger.info("Starting optimized Znuny sync")

        try:
            # Step 0: Sync details for ISP tickets that were just linked to Znuny
            # (have znuny_ticket_id but no znuny_created_by)
            unsynced_isp = self.db.get_tickets_needing_znuny_details()
            if unsynced_isp:
                logger.info(f"Syncing details for {len(unsynced_isp)} newly linked ISP tickets")
                for ticket in unsynced_isp:
                    try:
                        details = self.znuny_client.get_ticket_details(ticket.znuny_ticket_id)
                        if details:
                            self.db.update_znuny_details(
                                ticket.id,
                                znuny_created_at=details.created_at,
                                znuny_created_by=details.created_by,
                                znuny_address=details.address,
                                znuny_url=details.znuny_url
                            )
                            # Store articles
                            for article in details.articles:
                                self.db.upsert_znuny_article(
                                    ticket_id=ticket.id,
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
                                # Extract site visits
                                site_visit = parse_site_visit_article(article, ticket.znuny_ticket_id)
                                if site_visit:
                                    self.db.upsert_site_visit(
                                        znuny_ticket_id=site_visit.znuny_ticket_id,
                                        article_id=site_visit.article_number,
                                        site_type=site_visit.site_type,
                                        service_provider=site_visit.service_provider,
                                        scheduled_time=site_visit.scheduled_time,
                                        assigned_to=site_visit.assigned_to,
                                        visit_date=site_visit.visit_date,
                                        article_created_at=site_visit.article_created_at,
                                        ticket_id=ticket.id,
                                        znuny_url=details.znuny_url
                                    )
                                    results["site_visits_extracted"] += 1
                            results["isp_details_synced"] += 1
                    except Exception as e:
                        logger.error(f"Error syncing details for ticket {ticket.id}: {e}")
                        results["errors"] += 1

            # Step 1: Get open tickets from Znuny (uses TTL-based cache)
            all_tickets = self.znuny_client.get_open_tickets(force_refresh=force_refresh)
            results["znuny_tickets_found"] = len(all_tickets)
            logger.info(f"Found {len(all_tickets)} open tickets in Znuny")

            # Step 2: Get set of Znuny ticket IDs we've already fully synced
            # (have site visits extracted and no pending visits needing completion)
            synced_znuny_ids = self.db.get_synced_znuny_ticket_ids()

            # Step 3: Prioritize tickets - process those needing work first
            priority_tickets = []
            low_priority_tickets = []

            for ticket_info in all_tickets:
                znuny_id = ticket_info["ticket_number"]
                title = ticket_info.get("title", "").lower()

                # High priority: has "site visit" in title or not yet synced
                if "site visit" in title or znuny_id not in synced_znuny_ids:
                    priority_tickets.append(ticket_info)
                else:
                    low_priority_tickets.append(ticket_info)

            # Process high priority tickets first, then low priority
            ordered_tickets = priority_tickets + low_priority_tickets

            for ticket_info in ordered_tickets:
                try:
                    znuny_ticket_id = ticket_info["ticket_number"]
                    title = ticket_info.get("title", "")

                    # Try to extract ISP ticket ID from title
                    isp_info = self.znuny_client.extract_isp_ticket_id_from_title(title)
                    isp_ticket = None

                    # If we found an ISP ticket reference, try to link it
                    if isp_info["portal"] and isp_info["ticket_id"]:
                        isp_ticket = self.db.get_ticket_by_portal_id(
                            isp_info["portal"], isp_info["ticket_id"]
                        )
                        if isp_ticket:
                            # Update ISP ticket with Znuny link if not already linked
                            if not isp_ticket.znuny_ticket_id:
                                self.db.update_znuny_status(
                                    isp_ticket.id, True, znuny_ticket_id
                                )
                            results["isp_tickets_synced"] += 1

                    # Check if we can skip detail fetching for this ticket
                    # NEVER skip tickets with "site visit" in title - always check for new articles
                    has_site_visit_in_title = "site visit" in title.lower()
                    has_pending_visits = self.db.has_pending_site_visits(znuny_ticket_id)

                    # Only skip if:
                    # - No "site visit" in title (tickets with site visits need full processing)
                    # - Already synced (has site visits extracted before)
                    # - No pending visits needing completion
                    # - ISP ticket (if any) already has details synced
                    if (not has_site_visit_in_title and
                        znuny_ticket_id in synced_znuny_ids and
                        not has_pending_visits and
                        (not isp_ticket or isp_ticket.znuny_created_by)):
                        results["znuny_tickets_skipped"] += 1
                        continue

                    # Get detailed ticket info including articles
                    details = self.znuny_client.get_ticket_details(znuny_ticket_id)
                    if not details:
                        continue

                    results["znuny_tickets_processed"] += 1

                    # If linked to ISP ticket, update its Znuny details
                    if isp_ticket:
                        self.db.update_znuny_details(
                            isp_ticket.id,
                            znuny_created_at=details.created_at,
                            znuny_created_by=details.created_by,
                            znuny_address=details.address,
                            znuny_url=details.znuny_url
                        )

                    # Extract site visits from articles
                    for article in details.articles:
                        site_visit = parse_site_visit_article(article, znuny_ticket_id)
                        if site_visit:
                            self.db.upsert_site_visit(
                                znuny_ticket_id=site_visit.znuny_ticket_id,
                                article_id=site_visit.article_number,
                                site_type=site_visit.site_type,
                                service_provider=site_visit.service_provider,
                                scheduled_time=site_visit.scheduled_time,
                                assigned_to=site_visit.assigned_to,
                                visit_date=site_visit.visit_date,
                                article_created_at=site_visit.article_created_at,
                                ticket_id=isp_ticket.id if isp_ticket else None,
                                znuny_url=details.znuny_url
                            )
                            results["site_visits_extracted"] += 1

                            if isp_ticket:
                                results["site_visits_linked"] += 1

                            logger.info(
                                f"Extracted site visit from {znuny_ticket_id}: "
                                f"{site_visit.assigned_to} at {site_visit.scheduled_time}"
                            )

                        # Also store articles if linked to ISP ticket
                        if isp_ticket:
                            self.db.upsert_znuny_article(
                                ticket_id=isp_ticket.id,
                                znuny_ticket_id=znuny_ticket_id,
                                article_number=article.article_number,
                                sender=article.sender,
                                via=article.via,
                                subject=article.subject,
                                created_at=article.created_at,
                                created_at_str=article.created_at_str,
                                created_by=article.created_by,
                                body=article.body
                            )

                    # Check for follow-up articles to complete site visits
                    completed = self._complete_site_visits_by_followup(znuny_ticket_id, details.articles)
                    results["site_visits_completed"] += completed

                except Exception as e:
                    logger.error(f"Error processing Znuny ticket {ticket_info.get('ticket_number', '?')}: {e}")
                    results["errors"] += 1

        except Exception as e:
            logger.error(f"Error in comprehensive site visit sync: {e}")
            results["errors"] += 1

        logger.info(f"Site visit sync complete: {results}")
        return results

    def close(self):
        """Clean up resources."""
        if self._znuny_client:
            self._znuny_client.close()

"""
Znuny Service - Business logic for Znuny ticket synchronization.
"""

import time
from datetime import timedelta
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
            exists, znuny_id = self.znuny_client.check_ticket_sync(search_term)
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
                        znuny_url=details.znuny_url,
                        address=site_visit.address,
                        customer_name=site_visit.customer_name
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
        # Direct query for unchecked tickets (no full table scan)
        tickets = self.db.get_unchecked_tickets()

        results = {"checked": 0, "found": 0, "not_found": 0, "errors": 0}

        if not tickets:
            logger.info("No unchecked tickets to sync")
            return results

        logger.info(f"Checking {len(tickets)} tickets in Znuny")

        for ticket in tickets:
            search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id

            try:
                exists, znuny_id = self.znuny_client.check_ticket_sync(search_term)
                self.db.update_znuny_status(ticket.id, exists, znuny_id)
                results["checked"] += 1

                if exists:
                    results["found"] += 1
                    logger.info(f"Found {ticket.portal}/{ticket.ticket_id} in Znuny as {znuny_id}")
                else:
                    results["not_found"] += 1
            except Exception as e:
                logger.error(f"Error checking ticket {ticket.id}: {e}")
                self.db.log_system("error", "znuny", f"Error checking ticket {ticket.ticket_id} in Znuny: {e}")
                results["errors"] += 1

        logger.info(f"ISP ticket check complete: {results['found']}/{results['checked']} found in Znuny")
        return results

    def sync_all_znuny_details(self) -> Dict:
        """
        Sync Znuny details for all tickets that are in Znuny but missing details.

        Returns:
            Dict with sync statistics
        """
        # Direct query for active unsynced tickets (no full table scan)
        tickets = self.db.get_tickets_needing_znuny_details()

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
        # Single optimized query instead of loading all tickets into memory
        return self.db.get_sync_status_counts()

    def get_ticket_articles(self, ticket_id: int) -> List[Dict]:
        """Get Znuny articles for a ticket."""
        return self.db.get_znuny_articles(ticket_id=ticket_id)

    def check_all_tickets_in_znuny(self) -> Dict:
        """
        Check Znuny status for all tickets not yet marked as in Znuny.
        Alias for sync_unchecked_tickets with a different return format.

        Returns:
            Dict with checked and updated counts
        """
        result = self.sync_unchecked_tickets()
        return {
            "checked": result.get("checked", 0),
            "updated": result.get("found", 0)
        }

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
        max_article_num = max(a.article_number for a in sorted_articles) if sorted_articles else 0

        for visit in pending_visits:
            visit_article_id = visit["article_id"]

            # Check if there's ANY article with a higher article_number
            if max_article_num > visit_article_id:
                # Find the first article after the site visit article
                followup_time = None
                for article in sorted_articles:
                    if article.article_number > visit_article_id:
                        followup_time = article.created_at
                        break

                # Use followup article time, or current time if not available
                complete_time = followup_time or now_maldives()

                if self.db.complete_site_visit_by_followup(
                    znuny_ticket_id, visit_article_id, complete_time
                ):
                    completed_count += 1
                    logger.info(f"Site visit {visit_article_id} completed by follow-up article (max article: {max_article_num})")

        return completed_count

    def _ingest_swept_tickets(self, swept: list, results: dict) -> set:
        """Ingest a batch of creator-swept tickets (+ their articles) into
        znuny_tickets / znuny_articles. Returns the set of OPEN ticket numbers."""
        open_swept = set()
        for item in swept:
            try:
                details = item["details"]
                is_closed = item["state_type"] == "closed"
                znuny_ticket_id = details.ticket_number
                last_article = details.articles[-1] if details.articles else None

                # Normalize closed states ("Closed Successful" etc.) to 'closed'
                # so the app's LOWER(state) IN ('closed','resolved') logic matches.
                self.db.upsert_znuny_only_ticket({
                    "znuny_ticket_id": znuny_ticket_id,
                    "title": item["title"],
                    "state": "closed" if is_closed else details.state,
                    "queue": details.queue,
                    "priority": details.priority,
                    "owner": details.owner,
                    "created_at": details.created_at,
                    "created_by": details.created_by,
                    "closed_at": item["closed_at"],  # set when closed, None when open
                    "article_count": len(details.articles),
                    "last_article_by": last_article.created_by if last_article else None,
                    "last_article_at": last_article.created_at if last_article else None,
                    "znuny_url": details.znuny_url,
                    "isp_ticket_id": None,  # COALESCE preserves any existing ISP link
                })

                for article in details.articles:
                    self.db.upsert_znuny_article(
                        ticket_id=None,
                        znuny_ticket_id=znuny_ticket_id,
                        article_number=article.article_number,
                        sender=article.sender,
                        via=article.via,
                        subject=article.subject,
                        created_at=article.created_at,
                        created_at_str=article.created_at_str,
                        created_by=article.created_by,
                        body=article.body,
                    )

                if not is_closed:
                    open_swept.add(znuny_ticket_id)
            except Exception as e:
                logger.error(f"Creator sweep: error ingesting ticket {item.get('title','?')}: {e}")
                results["errors"] += 1
        return open_swept

    def _sync_by_creators(self, results: dict, force: bool = False) -> set:
        """Creator sweep: ingest tickets created by any tracked agent across ALL
        services and states (not just OAN/open), so the staff page reflects each
        agent's real output. Two tiers:

          - TODAY (every cycle): fetch tickets changed since the start of today —
            cheap, and keeps today's stats complete for ALL ticket types (the
            staff page computes today live from the DB).
          - CATCH-UP (once/day, gated by znuny_creator_sweep_date): a deeper
            incremental sweep from the rolling watermark to pick up changes on
            older tickets ("the other tickets"), then refresh recent snapshots.

        Tracked agents are the Znuny user ids harvested into ZnunyClient._user_names.
        mark_closed is decoupled (ISP-linked only), so the sweep need not feed the
        open list. Pass force=True to run the catch-up regardless of the gate.
        """
        user_ids = [uid for uid in ZnunyClient._user_names.keys() if str(uid).isdigit()]
        if not user_ids:
            logger.info("Creator sweep: no known agent user ids yet — skipping")
            return set()

        open_swept = set()
        today = now_maldives().date().isoformat()

        # Tier 1 — TODAY, all ticket types, every cycle.
        today_start = now_maldives().replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        today_items = self.znuny_client.get_tickets_by_creators(user_ids, changed_since=today_start)
        open_swept |= self._ingest_swept_tickets(today_items, results)
        results["creator_sweep_today"] = len(today_items)
        logger.info(f"Creator sweep (today) ingested {len(today_items)} tickets for {len(user_ids)} agents")

        # Tier 2 — deeper catch-up for older tickets, once per day.
        if force or self.db.get_setting("znuny_creator_sweep_date") != today:
            sweep_start = now_maldives()
            watermark = self.db.get_setting("znuny_creator_sweep_last")
            mode = f"incremental since {watermark}" if watermark else "full backfill"
            logger.info(f"Creator sweep (daily catch-up, {mode}) for {len(user_ids)} agents")
            items = self.znuny_client.get_tickets_by_creators(user_ids, changed_since=watermark)
            open_swept |= self._ingest_swept_tickets(items, results)

            # Watermark 2h before this run; mark the daily catch-up done for today.
            self.db.set_setting("znuny_creator_sweep_last",
                                (sweep_start - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                                "Last creator-sweep watermark (Znuny system time)")
            self.db.set_setting("znuny_creator_sweep_date", today,
                                "Date of the last completed daily creator catch-up")

            # Newly-ingested tickets may be dated to earlier days — refresh those
            # days' snapshots (last 14 days covers realistic late ingestion).
            for i in range(14):
                self.db.generate_staff_daily_snapshot(
                    (now_maldives().date() - timedelta(days=i)).isoformat())
            results["creator_sweep_daily"] = len(items)
            logger.info(f"Creator sweep (daily catch-up) ingested {len(items)} tickets")

        results["creator_sweep_open"] = len(open_swept)
        return open_swept

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
        UNIFIED comprehensive Znuny sync with 3-layer caching:

        Layer 1 (TTL cache): Non-critical tickets return cached details instantly (5 min TTL)
        Layer 2 (Article count): Navigate to page but skip parsing if article count unchanged
        Layer 3 (New articles only): When articles change, only process NEW articles

        Flow:
        0. Sync ISP tickets just linked to Znuny (new tickets needing initial details)
        1. Get open tickets from Znuny dashboard (5-min cache)
        1.5. Check unchecked ISP tickets against open tickets list
        1.7. Check completed ISP tickets (not in Znuny) by account number in closed tickets
        1.6. Handle closed tickets with pending site visits
        2. For each open ticket:
           a. Link to ISP tickets if applicable
           b. Get details (uses cache layers - instant for most tickets)
           c. Only process NEW articles (article_number > max known)
           d. Extract site visits, store articles, capture orphan tickets

        Returns sync statistics.
        """
        results = {
            "znuny_tickets_found": 0,
            "znuny_tickets_processed": 0,
            "znuny_tickets_skipped": 0,
            "znuny_only_captured": 0,
            "site_visits_extracted": 0,
            "site_visits_linked": 0,
            "site_visits_completed": 0,
            "site_visits_closed": 0,
            "isp_tickets_synced": 0,
            "isp_checked": 0,
            "isp_found": 0,
            "isp_details_synced": 0,
            "znuny_tickets_closed": 0,
            "errors": 0
        }

        logger.info("Starting unified Znuny sync")
        sync_start = time.time()

        # Track Znuny ticket IDs processed in Step 0 to avoid re-fetching in main loop
        step0_processed_ids = set()

        try:
            # Load known article counts for new-article detection
            known_article_counts = self.db.get_known_article_counts()

            # Step 0: Sync details for ISP tickets that were just linked to Znuny
            # (have znuny_ticket_id but no znuny_created_by)
            unsynced_isp = self.db.get_tickets_needing_znuny_details()
            if unsynced_isp:
                # Build title lookup dict once (avoids linear scan per ticket)
                open_tickets_map = {t["ticket_number"]: t.get("title") for t in self.znuny_client.get_open_tickets()}

                logger.info(f"Syncing details for {len(unsynced_isp)} newly linked ISP tickets")
                for ticket in unsynced_isp:
                    try:
                        details = self.znuny_client.get_ticket_details(ticket.znuny_ticket_id)
                        if details:
                            step0_processed_ids.add(ticket.znuny_ticket_id)
                            self.db.update_znuny_details(
                                ticket.id,
                                znuny_created_at=details.created_at,
                                znuny_created_by=details.created_by,
                                znuny_address=details.address,
                                znuny_url=details.znuny_url
                            )
                            # Filter to only new articles using known_article_counts
                            known = known_article_counts.get(ticket.znuny_ticket_id, {})
                            max_known_num = known.get("max_num", 0) or 0
                            articles_to_store = [a for a in details.articles if a.article_number > max_known_num] if max_known_num else details.articles

                            # Store articles
                            for article in articles_to_store:
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
                                        znuny_url=details.znuny_url,
                                        address=site_visit.address,
                                        customer_name=site_visit.customer_name
                                    )
                                    results["site_visits_extracted"] += 1

                            # Check for follow-up articles that complete pending site visits
                            self._complete_site_visits_by_followup(
                                ticket.znuny_ticket_id, details.articles
                            )

                            # Update known_article_counts so Step 2 doesn't re-process
                            known_article_counts[ticket.znuny_ticket_id] = {
                                "count": len(details.articles),
                                "max_num": max(a.article_number for a in details.articles) if details.articles else 0
                            }

                            # Also store in znuny_tickets table
                            last_art = details.articles[-1] if details.articles else None
                            self.db.upsert_znuny_only_ticket({
                                "znuny_ticket_id": ticket.znuny_ticket_id,
                                "title": open_tickets_map.get(ticket.znuny_ticket_id),
                                "state": details.state,
                                "queue": details.queue,
                                "priority": details.priority,
                                "owner": details.owner,
                                "created_at": details.created_at,
                                "created_by": details.created_by,
                                "article_count": len(details.articles),
                                "last_article_by": last_art.created_by if last_art else None,
                                "last_article_at": last_art.created_at if last_art else None,
                                "znuny_url": details.znuny_url,
                                "isp_ticket_id": ticket.id
                            })
                            results["isp_details_synced"] += 1
                    except Exception as e:
                        logger.error(f"Error syncing details for ticket {ticket.id}: {e}")
                        results["errors"] += 1

            # Step 1: Get open tickets from Znuny (uses TTL-based cache)
            all_tickets = self.znuny_client.get_open_tickets(force_refresh=force_refresh)
            results["znuny_tickets_found"] = len(all_tickets)
            logger.info(f"Found {len(all_tickets)} open tickets in Znuny")

            # Step 1.5: Check unchecked ISP tickets against open tickets list
            # Uses the already-loaded open tickets (no extra Selenium calls)
            unchecked_tickets = self.db.get_unchecked_tickets()
            if unchecked_tickets:
                logger.info(f"Checking {len(unchecked_tickets)} ISP tickets against Znuny open list")
                for ticket in unchecked_tickets:
                    search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id
                    try:
                        # search_by_title uses cached open tickets - no navigation
                        exists, znuny_id = self.znuny_client.check_ticket_sync(search_term)
                        self.db.update_znuny_status(ticket.id, exists, znuny_id)
                        results["isp_checked"] += 1
                        if exists:
                            results["isp_found"] += 1
                            logger.info(f"Found {ticket.portal}/{ticket.ticket_id} in Znuny as {znuny_id}")
                    except Exception as e:
                        logger.error(f"Error checking ticket {ticket.id}: {e}")
                        results["errors"] += 1

            # Step 1.7: Check completed ISP tickets not in Znuny by account number
            # These tickets disappeared from the portal but were never found in Znuny.
            # Search closed Znuny tickets by customer account as a last-chance check.
            # After 3 failed attempts, mark ticket as "Rejected on Portal".
            completed_not_in_znuny = self.db.get_completed_not_in_znuny()
            if completed_not_in_znuny:
                logger.info(f"Checking {len(completed_not_in_znuny)} completed ISP tickets by account in Znuny closed tickets")
                for ticket in completed_not_in_znuny:
                    try:
                        matches = self.znuny_client.search_closed_by_account(
                            ticket.account, ticket.ticket_id
                        )
                        if matches:
                            match = matches[0]
                            znuny_id = match["ticket_number"]
                            self.db.update_znuny_status(ticket.id, True, znuny_id)
                            # Store creation time and URL from search results
                            self.db.update_znuny_details(
                                ticket.id,
                                znuny_created_at=match.get("created_at"),
                                znuny_url=match.get("znuny_url"),
                            )
                            results["isp_found"] += 1
                            logger.info(
                                f"Account search: {ticket.portal}/{ticket.ticket_id} found "
                                f"in closed Znuny ticket {znuny_id}"
                                f" (created: {match.get('created_at')})"
                            )
                        else:
                            # Not found - increment search counter
                            count = self.db.increment_znuny_search_count(ticket.id)
                            if count >= 3:
                                self.db.mark_ticket_rejected(ticket.id)
                                logger.info(
                                    f"Account search: {ticket.portal}/{ticket.ticket_id} not found "
                                    f"after {count} attempts - marked as rejected on portal"
                                )
                            else:
                                logger.info(
                                    f"Account search: {ticket.portal}/{ticket.ticket_id} not found "
                                    f"(attempt {count}/3)"
                                )
                    except Exception as e:
                        logger.error(f"Error in account search for ticket {ticket.id}: {e}")
                        results["errors"] += 1

            # Step 1.6: Check for closed tickets with pending site visits
            # If a Znuny ticket with pending visits is no longer in open list, it's closed
            # Safety: Only run if we actually got open tickets (0 = likely page load failure)
            open_znuny_ids = {t["ticket_number"] for t in all_tickets}
            if not open_znuny_ids:
                logger.warning("No open tickets found - skipping closed ticket check (possible page load failure)")

            pending_visit_ids = self.db.get_znuny_ids_with_pending_visits() if open_znuny_ids else set()
            closed_with_pending = pending_visit_ids - open_znuny_ids

            if closed_with_pending:
                logger.info(f"Found {len(closed_with_pending)} closed tickets with pending visits")
                for closed_id in closed_with_pending:
                    # Fetch ticket details to get the last article time
                    try:
                        details = self.znuny_client.get_ticket_details(closed_id, skip_body_fetch=True)
                        if details and details.articles:
                            # Use the last article's created_at as completion time
                            last_article = max(details.articles, key=lambda a: a.article_number)
                            if last_article.created_at:
                                logger.info(f"Using last article time {last_article.created_at} for closed ticket {closed_id}")
                                count = self.db.complete_site_visits_for_closed_ticket(
                                    closed_id, completed_at=last_article.created_at
                                )
                            else:
                                count = self.db.complete_site_visits_for_closed_ticket(closed_id)
                        else:
                            count = self.db.complete_site_visits_for_closed_ticket(closed_id)
                        results["site_visits_closed"] += count
                    except Exception as e:
                        logger.error(f"Error fetching details for closed ticket {closed_id}: {e}")
                        count = self.db.complete_site_visits_for_closed_ticket(closed_id)
                        results["site_visits_closed"] += count

            # Derive remaining pending visit IDs from Step 1.6 (no extra DB query needed)
            all_pending_visit_ids = pending_visit_ids - closed_with_pending

            # Warm the per-ticket details cache in a few batched TicketGet calls so the
            # Step 2 loop below is served from cache instead of one request per ticket.
            self.znuny_client.prefetch_open_ticket_details()

            # Step 2: Process all open tickets (queue view data is embedded in ticket_info)
            for ticket_idx, ticket_info in enumerate(all_tickets, 1):
                try:
                    znuny_ticket_id = ticket_info["ticket_number"]
                    title = ticket_info.get("title", "")
                    ticket_start = time.time()

                    # Try to extract ISP ticket ID from title and link
                    isp_info = self.znuny_client.extract_isp_ticket_id_from_title(title)
                    isp_ticket = None

                    if isp_info["portal"] and isp_info["ticket_id"]:
                        isp_ticket = self.db.get_ticket_by_portal_id(
                            isp_info["portal"], isp_info["ticket_id"]
                        )
                        if isp_ticket:
                            if not isp_ticket.znuny_ticket_id:
                                self.db.update_znuny_status(
                                    isp_ticket.id, True, znuny_ticket_id
                                )
                            results["isp_tickets_synced"] += 1

                    # Skip if already processed in Step 0
                    if znuny_ticket_id in step0_processed_ids:
                        results["znuny_tickets_skipped"] += 1
                        continue

                    # Determine if this ticket needs frequent checking
                    has_site_visit_in_title = "site visit" in title.lower()
                    has_pending_visits = znuny_ticket_id in all_pending_visit_ids
                    needs_frequent_check = has_site_visit_in_title or has_pending_visits

                    # Get ticket details with appropriate cache strategy:
                    # - Frequent-check tickets: bypass TTL cache (but article-count check still works)
                    # - Other tickets: use TTL cache (instant return within 5 min)
                    logger.info(f"Step 2: ticket {ticket_idx}/{len(all_tickets)} {znuny_ticket_id} - fetching details")
                    details = self.znuny_client.get_ticket_details(
                        znuny_ticket_id,
                        bypass_cache=needs_frequent_check
                    )
                    if not details:
                        logger.warning(f"Step 2: ticket {znuny_ticket_id} - no details returned ({time.time() - ticket_start:.1f}s)")
                        continue

                    # Always store/update in znuny_tickets (before any skip checks)
                    # This ensures ALL open tickets are catalogued with ISP link status
                    # Sidebar-parsed data takes priority over queue view data from ticket_info
                    last_article = details.articles[-1] if details.articles else None
                    self.db.upsert_znuny_only_ticket({
                        "znuny_ticket_id": znuny_ticket_id,
                        "title": title,
                        "state": details.state or ticket_info.get("state"),
                        "queue": details.queue or ticket_info.get("queue"),
                        "priority": details.priority or ticket_info.get("priority"),
                        "owner": details.owner or ticket_info.get("owner"),
                        "created_at": details.created_at,
                        "created_by": details.created_by,
                        "closed_at": None,  # Open ticket
                        "article_count": len(details.articles),
                        "last_article_by": last_article.created_by if last_article else None,
                        "last_article_at": last_article.created_at if last_article else None,
                        "znuny_url": details.znuny_url,
                        "isp_ticket_id": isp_ticket.id if isp_ticket else None
                    })
                    results["znuny_only_captured"] += 1

                    # Determine which articles are NEW (not yet in our DB)
                    known = known_article_counts.get(znuny_ticket_id, {})
                    max_known_num = known.get("max_num", 0) or 0
                    is_first_time = max_known_num == 0

                    new_articles = [
                        a for a in details.articles
                        if a.article_number > max_known_num
                    ] if not is_first_time else details.articles

                    # If no new articles and ISP details already synced → skip article processing
                    # But still check for follow-up completions on tickets with pending visits
                    if (not new_articles and not is_first_time and
                            (not isp_ticket or isp_ticket.znuny_created_by)):
                        if has_pending_visits:
                            completed = self._complete_site_visits_by_followup(znuny_ticket_id, details.articles)
                            results["site_visits_completed"] += completed
                        results["znuny_tickets_skipped"] += 1
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

                    # Process only NEW articles (or all if first time)
                    articles_to_process = new_articles if not is_first_time else details.articles
                    if new_articles and not is_first_time:
                        logger.info(f"Ticket {znuny_ticket_id}: {len(new_articles)} new articles (max known: {max_known_num})")

                    for article in articles_to_process:
                        # Extract site visits
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
                                znuny_url=details.znuny_url,
                                address=site_visit.address,
                                customer_name=site_visit.customer_name
                            )
                            results["site_visits_extracted"] += 1

                            if isp_ticket:
                                results["site_visits_linked"] += 1

                            logger.info(
                                f"Extracted site visit from {znuny_ticket_id}: "
                                f"{site_visit.assigned_to} at {site_visit.scheduled_time}"
                            )

                        # Store articles for ALL tickets (not just ISP-linked)
                        # This enables article-count tracking for change detection
                        self.db.upsert_znuny_article(
                            ticket_id=isp_ticket.id if isp_ticket else None,
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
                    if has_pending_visits or new_articles:
                        completed = self._complete_site_visits_by_followup(znuny_ticket_id, details.articles)
                        results["site_visits_completed"] += completed

                except Exception as e:
                    ticket_num = ticket_info.get('ticket_number', '?')
                    logger.error(f"Error processing Znuny ticket {ticket_num}: {e}")
                    self.db.log_system("error", "znuny", f"Error processing ticket {ticket_num}: {e}")
                    results["errors"] += 1

            # Step 2 complete
            logger.info(f"Step 2 complete: processed={results['znuny_tickets_processed']}, skipped={results['znuny_tickets_skipped']}, errors={results['errors']}")

            # Step 2.7: Creator sweep — ingest each tracked agent's full ticket
            # set (all services/states). Add its OPEN tickets to open_znuny_ids so
            # Step 3 does not wrongly close non-OAN open tickets.
            try:
                open_swept = self._sync_by_creators(results)
                open_znuny_ids |= open_swept
            except Exception as e:
                logger.error(f"Creator sweep failed: {e}")
                self.db.log_system("error", "znuny", f"Creator sweep failed: {e}")
                results["errors"] += 1

            # Step 3: Mark znuny_tickets as closed if no longer in open list
            if open_znuny_ids:
                closed_count = self.db.mark_znuny_tickets_closed(open_znuny_ids)
                results["znuny_tickets_closed"] = closed_count

        except Exception as e:
            logger.error(f"Error in comprehensive site visit sync: {e}")
            self.db.log_system("error", "znuny", f"Znuny sync error: {e}")
            results["errors"] += 1

        # Log Znuny browser memory and timing at end of sync
        elapsed = time.time() - sync_start
        results["sync_duration_seconds"] = round(elapsed, 1)
        try:
            mem_mb = self.znuny_client._get_browser_memory_mb()
            if mem_mb > 0:
                results["znuny_memory_mb"] = round(mem_mb, 1)
                logger.info(f"Znuny browser memory: {mem_mb:.0f}MB")
        except Exception:
            pass

        logger.info(f"Site visit sync complete ({elapsed:.1f}s): {results}")
        return results

    def close(self):
        """Clean up resources."""
        if self._znuny_client:
            self._znuny_client.close()

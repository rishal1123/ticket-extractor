import argparse
import signal
import sys
import threading
import time

import schedule

from config import Config
from database import Database
from extractors import DhiraaguExtractor, OoredooExtractor, ROLExtractor, MedianetExtractor
from znuny_client import ZnunyClient
from utils.logger import setup_logger, get_logger

logger = get_logger("main")

# Global flag for graceful shutdown
running = True


def signal_handler(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


def get_extractor_class(portal_name: str):
    """Get the extractor class for a portal name."""
    extractors = {
        "dhiraagu": DhiraaguExtractor,
        "ooredoo": OoredooExtractor,
        "rol": ROLExtractor,
        "medianet": MedianetExtractor
    }
    return extractors.get(portal_name.lower())


def run_extraction(portal_name: str = None, headless: bool = False):
    """Run extraction for one or all portals."""
    db = Database()
    results = []

    if portal_name:
        # Run single portal
        config = Config.get_portal_by_name(portal_name)
        if not config:
            logger.error(f"Unknown portal: {portal_name}")
            return results

        extractor_class = get_extractor_class(portal_name)
        if not extractor_class:
            logger.error(f"No extractor found for portal: {portal_name}")
            return results

        if not config.url or not config.username:
            logger.warning(f"Portal {portal_name} not configured, skipping")
            return results

        extractor = extractor_class(config, db, headless=headless)
        result = extractor.run()
        results.append(result)
    else:
        # Run all portals
        for config in Config.get_all_portals():
            if not config.url or not config.username:
                logger.warning(f"Portal {config.name} not configured, skipping")
                continue

            extractor_class = get_extractor_class(config.name)
            if not extractor_class:
                logger.warning(f"No extractor for {config.name}, skipping")
                continue

            logger.info(f"Starting extraction for {config.name}")
            extractor = extractor_class(config, db, headless=headless)
            result = extractor.run()
            results.append(result)

            # Small delay between portals
            if running:
                time.sleep(5)

    return results


def sync_znuny_for_new_tickets(db: Database):
    """Sync Znuny status and details for tickets not yet checked."""
    logger.info("Starting Znuny sync for unchecked tickets")
    znuny_client = ZnunyClient()

    tickets = db.get_all_tickets()
    tickets_to_check = [t for t in tickets if not t.in_znuny]

    updated = 0
    for ticket in tickets_to_check:
        try:
            # Use account for ROL, ticket_id for others
            search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id
            exists, znuny_ticket_id = znuny_client.check_ticket_sync(
                search_term,
                ticket.customer_name
            )
            if exists:
                db.update_znuny_status(ticket.id, True, znuny_ticket_id)
                updated += 1

                # Fetch details
                details = znuny_client.get_ticket_details(znuny_ticket_id)
                if details:
                    db.update_znuny_details(
                        ticket.id,
                        znuny_created_at=details.created_at,
                        znuny_created_by=details.created_by,
                        znuny_address=details.address
                    )
                    for article in details.articles:
                        db.upsert_znuny_article(
                            ticket_id=ticket.id,
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
        except Exception as e:
            logger.warning(f"Error checking ticket {ticket.ticket_id}: {e}")
            continue

    logger.info(f"Znuny sync complete: {updated} tickets found in Znuny")
    return updated


def scheduled_extraction():
    """Function called by scheduler."""
    logger.info("Starting scheduled extraction")
    db = Database()
    results = run_extraction(headless=True)  # Run in headless mode (background)

    # Summary
    total_found = sum(r.get("tickets_found", 0) for r in results)
    total_new = sum(r.get("tickets_new", 0) for r in results)
    total_updated = sum(r.get("tickets_updated", 0) for r in results)
    failed = [r["portal"] for r in results if r["status"] == "failed"]

    logger.info(f"Extraction complete: {total_found} found, {total_new} new, {total_updated} updated")
    if failed:
        logger.warning(f"Failed portals: {', '.join(failed)}")

    # Sync Znuny for newly found tickets
    try:
        sync_znuny_for_new_tickets(db)
    except Exception as e:
        logger.error(f"Znuny sync failed: {e}")


def run_scheduler():
    """Run the extraction scheduler."""
    global running

    interval = Config.EXTRACTION_INTERVAL_MINUTES
    logger.info(f"Starting scheduler with {interval} minute interval")

    # Run immediately on start
    scheduled_extraction()

    # Schedule periodic runs
    schedule.every(interval).minutes.do(scheduled_extraction)

    while running:
        schedule.run_pending()
        time.sleep(1)

    logger.info("Scheduler stopped")


def run_dashboard_thread():
    """Run dashboard in a separate thread."""
    from dashboard import run_dashboard
    run_dashboard()


def main():
    parser = argparse.ArgumentParser(description="Ticket Extractor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run extraction once and exit"
    )
    parser.add_argument(
        "--portal",
        type=str,
        choices=["dhiraagu", "ooredoo", "rol", "medianet"],
        help="Run extraction for specific portal only"
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Run only the dashboard without extraction"
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Run extraction without starting the dashboard"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)"
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Run browser in visible mode (disable headless)"
    )

    args = parser.parse_args()

    # Setup logging
    setup_logger()
    logger.info("Ticket Extractor starting...")

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.dashboard_only:
            # Run only dashboard
            logger.info("Starting dashboard only mode")
            from dashboard import run_dashboard
            run_dashboard()

        elif args.once:
            # Run once and exit
            logger.info("Running single extraction")
            headless = not args.visible  # Default headless unless --visible specified
            results = run_extraction(portal_name=args.portal, headless=headless)

            # Print summary
            print("\n" + "=" * 60)
            print("EXTRACTION SUMMARY")
            print("=" * 60)
            for r in results:
                status_icon = "OK" if r["status"] == "success" else "FAILED"
                print(f"{r['portal']:12} [{status_icon}] Found: {r['tickets_found']:3}, "
                      f"New: {r['tickets_new']:3}, Updated: {r['tickets_updated']:3}, "
                      f"Completed: {r.get('tickets_completed', 0):3}")
                if r.get("error"):
                    print(f"             Error: {r['error']}")
            print("=" * 60)

        else:
            # Run scheduler with dashboard
            if not args.no_dashboard:
                # Start dashboard - it has its own scheduler built-in via lifespan
                # So we don't need to run our own scheduler when dashboard is enabled
                logger.info(f"Starting dashboard with built-in scheduler at http://{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}")
                from dashboard import run_dashboard
                run_dashboard()  # This blocks and runs the dashboard with its scheduler
            else:
                # Run scheduler in main thread (no dashboard)
                run_scheduler()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

    logger.info("Ticket Extractor stopped")


if __name__ == "__main__":
    main()

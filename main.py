"""
Ticket Extractor - Main Entry Point

This module provides the CLI interface for the application.
The actual application logic follows MVC pattern:
- Controllers: Handle HTTP requests (controllers/)
- Services: Business logic (services/)
- Models: Data structures (models/)
- Database: Data access layer (database.py)
"""

import argparse
import signal
import sys
import time

import schedule

from config import Config
from database import Database, now_maldives
from services import ExtractionService, ZnunyService
from utils.logger import setup_logger, get_logger

logger = get_logger("main")

# Global flag for graceful shutdown
running = True


def signal_handler(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


def run_extraction(portal_name: str = None, headless: bool = False):
    """Run extraction for one or all portals using ExtractionService."""
    db = Database()
    extraction_service = ExtractionService(db)

    if portal_name:
        result = extraction_service.extract_from_portal(portal_name, headless=headless)
        return [result]
    else:
        results = extraction_service.extract_from_all_portals(headless=headless)
        return list(results.get("portals", {}).values())


def sync_znuny_for_new_tickets(db: Database):
    """Sync Znuny status and details for tickets not yet checked using ZnunyService."""
    logger.info("Starting Znuny sync for unchecked tickets")
    znuny_service = ZnunyService(db)
    result = znuny_service.sync_unchecked_tickets()
    logger.info(f"Znuny sync complete: {result}")
    return result.get("found", 0)


def scheduled_extraction():
    """Function called by scheduler."""
    logger.info("Starting scheduled extraction")
    db = Database()

    # Run extraction
    results = run_extraction(headless=True)

    # Summary
    total_found = sum(r.get("tickets_found", 0) for r in results)
    total_new = sum(r.get("tickets_new", 0) for r in results)
    total_updated = sum(r.get("tickets_updated", 0) for r in results)
    failed = [r["portal"] for r in results if r.get("status") == "failed"]

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

    interval = Config.get_extraction_interval()
    logger.info(f"Starting scheduler with {interval} minute interval")

    # Run immediately on start
    scheduled_extraction()

    # Schedule periodic runs
    schedule.every(interval).minutes.do(scheduled_extraction)

    while running:
        schedule.run_pending()
        time.sleep(1)

    logger.info("Scheduler stopped")


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
            from app import run_app
            run_app()

        elif args.once:
            # Run once and exit
            logger.info("Running single extraction")
            headless = not args.visible
            results = run_extraction(portal_name=args.portal, headless=headless)

            # Print summary
            print("\n" + "=" * 60)
            print("EXTRACTION SUMMARY")
            print("=" * 60)
            for r in results:
                status_icon = "OK" if r.get("status") == "success" else "FAILED"
                print(f"{r['portal']:12} [{status_icon}] Found: {r.get('tickets_found', 0):3}, "
                      f"New: {r.get('tickets_new', 0):3}, Updated: {r.get('tickets_updated', 0):3}, "
                      f"Completed: {r.get('tickets_completed', 0):3}")
                if r.get("error"):
                    print(f"             Error: {r['error']}")
            print("=" * 60)

        else:
            # Run scheduler with dashboard
            if not args.no_dashboard:
                logger.info(f"Starting dashboard at http://{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}")
                from app import run_app
                run_app()
            else:
                # Run scheduler in main thread (no dashboard)
                run_scheduler()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)

    logger.info("Ticket Extractor stopped")


if __name__ == "__main__":
    main()

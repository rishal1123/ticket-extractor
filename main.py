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

from config import Config
from database import Database
from services import ExtractionService
from services.scheduler_service import get_scheduler
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


def run_scheduler():
    """
    Run the background scheduler in the foreground, without the web server.

    Delegates to services.scheduler_service.SchedulerService -- the same
    scheduler app.py's lifespan starts -- instead of a second, divergent
    schedule/extraction/sync loop. SchedulerService.start() runs its own
    background thread, so this just blocks the main thread until a shutdown
    signal flips `running` (see signal_handler above), then stops it.
    """
    global running

    scheduler = get_scheduler()
    scheduler.start()
    logger.info("Scheduler started (no-dashboard mode)")

    while running:
        time.sleep(1)

    scheduler.stop()
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

"""
Scheduler Service - Handles background extraction and sync jobs.

This service encapsulates all scheduler-related logic including:
- Running portal extractions
- Running Znuny sync
- Managing the background scheduler thread
"""

import time
import threading
from typing import Callable, Optional

import schedule

from database import Database
from config import Config
from services.znuny_service import ZnunyService
from extractors import DhiraaguExtractor, OoredooExtractor, ROLExtractor, MedianetExtractor
from utils.logger import get_logger

logger = get_logger("scheduler")


def get_extractor_class(portal_name: str):
    """Get the extractor class for a portal name."""
    extractors = {
        "dhiraagu": DhiraaguExtractor,
        "ooredoo": OoredooExtractor,
        "rol": ROLExtractor,
        "medianet": MedianetExtractor
    }
    return extractors.get(portal_name.lower())


class SchedulerService:
    """Service for managing background extraction and sync jobs."""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interval_minutes = Config.EXTRACTION_INTERVAL_MINUTES

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def run_portal_extraction(self) -> list[dict]:
        """Run extraction for all configured portals."""
        logger.info("Starting scheduled portal extraction")
        db = Database()
        db.log_system("info", "scheduler", "Portal extraction started")
        results = []

        for config in Config.get_all_portals():
            if not config.url or not config.username:
                logger.warning(f"Portal {config.name} not configured, skipping")
                db.log_system("warning", f"extractor.{config.name}", "Portal not configured, skipping")
                continue

            extractor_class = get_extractor_class(config.name)
            if not extractor_class:
                logger.warning(f"No extractor for {config.name}, skipping")
                continue

            try:
                logger.info(f"Extracting from {config.name}")
                extractor = extractor_class(config, db, headless=True)
                result = extractor.run()
                results.append(result)
                db.log_system(
                    "info", f"extractor.{config.name}",
                    f"Extraction complete: {result.get('tickets_found', 0)} found, {result.get('tickets_new', 0)} new",
                    f"Status: {result.get('status')}"
                )
            except Exception as e:
                logger.error(f"Extraction failed for {config.name}: {e}")
                results.append({"portal": config.name, "status": "failed", "error": str(e)})
                db.log_system("error", f"extractor.{config.name}", f"Extraction failed: {e}")

            time.sleep(2)  # Small delay between portals

        # Summary
        total_found = sum(r.get("tickets_found", 0) for r in results)
        total_new = sum(r.get("tickets_new", 0) for r in results)
        failed = [r["portal"] for r in results if r.get("status") == "failed"]

        logger.info(f"Portal extraction complete: {total_found} found, {total_new} new")
        summary = f"Total: {total_found} found, {total_new} new"
        if failed:
            logger.warning(f"Failed portals: {', '.join(failed)}")
            summary += f", Failed: {', '.join(failed)}"
            db.log_system("warning", "scheduler", f"Extraction completed with failures", summary)
        else:
            db.log_system("info", "scheduler", "Extraction completed successfully", summary)

        return results

    def run_znuny_sync(self) -> dict:
        """Run comprehensive Znuny sync."""
        logger.info("Starting comprehensive Znuny sync")
        db = Database()
        db.log_system("info", "znuny", "Comprehensive Znuny sync started")

        try:
            znuny_service = ZnunyService(db)
            results = znuny_service.sync_all_site_visits()

            logger.info(f"Znuny sync complete: {results}")
            db.log_system(
                "info" if results["errors"] == 0 else "warning",
                "znuny",
                f"Znuny sync complete: {results['znuny_tickets_found']} tickets, {results['site_visits_extracted']} site visits",
                f"Tickets: {results['znuny_tickets_found']}, ISP synced: {results['isp_tickets_synced']}, "
                f"Site visits: {results['site_visits_extracted']}, Linked: {results['site_visits_linked']}, "
                f"Errors: {results['errors']}"
            )
            return results
        except Exception as e:
            logger.error(f"Comprehensive Znuny sync failed: {e}")
            db.log_system("error", "znuny", f"Znuny sync failed: {e}")
            raise

    def _scheduled_job(self):
        """Combined job that runs portal extraction and comprehensive Znuny sync."""
        db = Database()
        try:
            self.run_portal_extraction()
        except Exception as e:
            logger.error(f"Portal extraction failed: {e}")
            db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")

        try:
            self.run_znuny_sync()
        except Exception as e:
            logger.error(f"Znuny sync failed: {e}")
            db.log_system("error", "scheduler", f"Znuny sync crashed: {e}")

    def _scheduler_loop(self):
        """Background scheduler loop."""
        logger.info(f"Scheduler started with {self._interval_minutes} minute interval")

        db = Database()
        db.log_system("info", "scheduler", f"Scheduler started with {self._interval_minutes} minute interval")

        # Schedule the job
        schedule.every(self._interval_minutes).minutes.do(self._scheduled_job)

        # Run immediately on start
        self._scheduled_job()

        while self._running:
            schedule.run_pending()
            time.sleep(1)

        logger.info("Scheduler stopped")
        db.log_system("info", "scheduler", "Scheduler stopped")

    def start(self):
        """Start the background scheduler thread."""
        if self.is_running:
            logger.info("Scheduler already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._scheduler_loop,
            daemon=True,
            name="ExtractionScheduler"
        )
        self._thread.start()
        logger.info("Background scheduler thread started")

    def stop(self):
        """Stop the background scheduler."""
        self._running = False
        logger.info("Scheduler stop requested")

    def get_status(self) -> dict:
        """Get scheduler status."""
        return {
            "running": self.is_running,
            "interval_minutes": self._interval_minutes,
            "thread_alive": self._thread.is_alive() if self._thread else False
        }


# Global scheduler instance
_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler

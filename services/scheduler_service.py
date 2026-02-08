"""
Scheduler Service - Handles background extraction and sync jobs.

This service encapsulates all scheduler-related logic including:
- Running portal extractions (every N minutes, default 5)
- Running Znuny sync (every M minutes, default 1) as a separate job
- Managing the background scheduler thread
"""

import time
import threading
from typing import Callable, Optional

import schedule

from database import Database
from config import Config
from services.znuny_service import ZnunyService
from services.extraction_service import ExtractionService
from utils.logger import get_logger

logger = get_logger("scheduler")


class SchedulerService:
    """Service for managing background extraction and sync jobs."""

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._extraction_interval = Config.EXTRACTION_INTERVAL_MINUTES
        self._znuny_sync_interval = Config.ZNUNY_SYNC_INTERVAL_MINUTES
        self._znuny_sync_lock = threading.Lock()
        self._extraction_lock = threading.Lock()

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

            extractor_class = ExtractionService.PORTAL_EXTRACTORS.get(config.name.lower())
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
        """Run comprehensive Znuny sync: check ISP tickets + sync site visits."""
        logger.info("Starting comprehensive Znuny sync")
        db = Database()
        db.log_system("info", "znuny", "Comprehensive Znuny sync started")

        try:
            znuny_service = ZnunyService(db)

            # Step 1: Check if ISP tickets exist in Znuny (get their Znuny IDs)
            logger.info("Checking ISP tickets in Znuny...")
            check_results = znuny_service.sync_unchecked_tickets()
            logger.info(f"ISP check complete: {check_results['checked']} checked, {check_results['found']} found in Znuny")
            db.log_system(
                "info",
                "znuny",
                f"ISP ticket check: {check_results['found']}/{check_results['checked']} found in Znuny"
            )

            # Step 2: Sync site visits and details from Znuny
            logger.info("Syncing site visits from Znuny...")
            results = znuny_service.sync_all_site_visits()

            # Combine results
            results["isp_checked"] = check_results["checked"]
            results["isp_found"] = check_results["found"]

            logger.info(f"Znuny sync complete: {results}")
            db.log_system(
                "info" if results["errors"] == 0 else "warning",
                "znuny",
                f"Znuny sync complete: {results['znuny_tickets_found']} tickets, {results['site_visits_extracted']} site visits",
                f"ISP checked: {check_results['checked']}, ISP found: {check_results['found']}, "
                f"Site visits: {results['site_visits_extracted']}, Linked: {results['site_visits_linked']}, "
                f"Errors: {results['errors']}"
            )
            return results
        except Exception as e:
            logger.error(f"Comprehensive Znuny sync failed: {e}")
            db.log_system("error", "znuny", f"Znuny sync failed: {e}")
            raise

    def _extraction_worker(self):
        """Run portal extraction with lock to prevent overlap."""
        if not self._extraction_lock.acquire(blocking=False):
            logger.debug("Portal extraction still running, skipping this cycle")
            return

        try:
            db = Database()
            try:
                self.run_portal_extraction()
            except Exception as e:
                logger.error(f"Portal extraction failed: {e}")
                db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")
        finally:
            self._extraction_lock.release()

    def _znuny_sync_worker(self):
        """Run Znuny sync with lock to prevent overlap."""
        if not self._znuny_sync_lock.acquire(blocking=False):
            logger.debug("Znuny sync still running, skipping this cycle")
            return

        try:
            db = Database()
            try:
                self.run_znuny_sync()
            except Exception as e:
                logger.error(f"Znuny sync failed: {e}")
                db.log_system("error", "scheduler", f"Znuny sync crashed: {e}")
        finally:
            self._znuny_sync_lock.release()

    def _extraction_job(self):
        """Launch portal extraction in its own thread (non-blocking)."""
        threading.Thread(
            target=self._extraction_worker,
            daemon=True,
            name="ExtractionWorker"
        ).start()

    def _znuny_sync_job(self):
        """Launch Znuny sync in its own thread (non-blocking)."""
        threading.Thread(
            target=self._znuny_sync_worker,
            daemon=True,
            name="ZnunySyncWorker"
        ).start()

    def _scheduler_loop(self):
        """Background scheduler loop with separate extraction and sync jobs."""
        logger.info(
            f"Scheduler started: extraction every {self._extraction_interval}min, "
            f"Znuny sync every {self._znuny_sync_interval}min"
        )

        db = Database()
        db.log_system(
            "info", "scheduler",
            f"Scheduler started: extraction every {self._extraction_interval}min, "
            f"Znuny sync every {self._znuny_sync_interval}min"
        )

        # Schedule separate jobs (each runs in its own thread)
        schedule.every(self._extraction_interval).minutes.do(self._extraction_job)
        schedule.every(self._znuny_sync_interval).minutes.do(self._znuny_sync_job)

        # Run both immediately on start (in parallel threads)
        self._extraction_job()
        self._znuny_sync_job()

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
            "extraction_interval_minutes": self._extraction_interval,
            "znuny_sync_interval_minutes": self._znuny_sync_interval,
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

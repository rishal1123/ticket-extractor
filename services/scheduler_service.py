"""
Scheduler Service - Handles background extraction and sync jobs.

This service encapsulates all scheduler-related logic including:
- Running portal extractions (every N minutes, default 5)
- Running Znuny sync (every M minutes, default 1) as a separate job
- Managing the background scheduler thread

Uses persistent worker threads so Playwright browser instances can be
reused across cycles (Playwright objects are bound to their creator thread).
"""

import time
import threading
from typing import Optional

import psutil
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
        self._extraction_interval = Config.get_extraction_interval()
        self._znuny_sync_interval = Config.get_znuny_sync_interval()
        # Events to signal persistent worker threads
        self._extraction_event = threading.Event()
        self._znuny_sync_event = threading.Event()
        # Persistent worker threads (keep Playwright alive across cycles)
        self._extraction_thread: Optional[threading.Thread] = None
        self._znuny_sync_thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running and self._thread is not None and self._thread.is_alive()

    def _has_db_credentials(self) -> bool:
        """Check if portal credentials exist in the database."""
        db = Database()
        config = db.get_config_settings()
        # Check if at least one portal has URL + username configured
        for portal in ("DHIRAAGU", "OOREDOO", "ROL", "MEDIANET"):
            if config.get(f"{portal}_URL") and config.get(f"{portal}_USERNAME"):
                return True
        return False

    def run_portal_extraction(self) -> list[dict]:
        """Run extraction for all configured portals."""
        if not self._has_db_credentials():
            logger.warning("No portal credentials in database - skipping extraction. Upload .env via Admin > Config.")
            return []

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

        # Log per-portal and total memory usage
        mem_parts = []
        for r in results:
            if r.get("memory_mb"):
                mem_parts.append(f"{r['portal']}:{r['memory_mb']}MB")
        total_mem = sum(r.get("memory_mb", 0) for r in results)
        mem_str = f", Memory: {', '.join(mem_parts)} (total: {total_mem:.0f}MB)" if mem_parts else ""

        # Log process-level memory
        proc_mem = psutil.Process().memory_info().rss / (1024 * 1024)
        mem_str += f", App: {proc_mem:.0f}MB"

        logger.info(f"Portal extraction complete: {total_found} found, {total_new} new{mem_str}")
        summary = f"Total: {total_found} found, {total_new} new{mem_str}"
        if failed:
            logger.warning(f"Failed portals: {', '.join(failed)}")
            summary += f", Failed: {', '.join(failed)}"
            db.log_system("warning", "scheduler", f"Extraction completed with failures", summary)
        else:
            db.log_system("info", "scheduler", "Extraction completed successfully", summary)

        return results

    def run_znuny_sync(self) -> dict:
        """Run unified Znuny sync: ISP check + site visits + articles in one pass."""
        if not self._has_db_credentials():
            logger.warning("No credentials in database - skipping Znuny sync. Upload .env via Admin > Config.")
            return {}

        logger.info("Starting unified Znuny sync")
        db = Database()
        db.log_system("info", "znuny", "Znuny sync started")

        try:
            znuny_service = ZnunyService(db)
            results = znuny_service.sync_all_site_visits()

            logger.info(f"Znuny sync complete: {results}")
            db.log_system(
                "info" if results["errors"] == 0 else "warning",
                "znuny",
                f"Znuny sync complete: {results['znuny_tickets_found']} tickets, "
                f"{results['site_visits_extracted']} site visits, "
                f"ISP {results['isp_found']}/{results['isp_checked']} found",
                f"Processed: {results['znuny_tickets_processed']}, "
                f"Skipped: {results['znuny_tickets_skipped']}, "
                f"Errors: {results['errors']}"
            )
            return results
        except Exception as e:
            logger.error(f"Znuny sync failed: {e}")
            db.log_system("error", "znuny", f"Znuny sync failed: {e}")
            raise

    def _extraction_worker_loop(self):
        """Persistent extraction worker - stays alive between cycles.

        Playwright objects are bound to their creator thread, so keeping
        this thread alive allows browser reuse across extraction cycles.
        """
        logger.info("Extraction worker thread started")
        while self._running:
            self._extraction_event.wait(timeout=5)
            if not self._running:
                break
            if not self._extraction_event.is_set():
                continue
            self._extraction_event.clear()

            try:
                self.run_portal_extraction()
            except Exception as e:
                logger.error(f"Portal extraction failed: {e}")
                db = Database()
                db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")

        logger.info("Extraction worker thread stopped")

    def _znuny_sync_worker_loop(self):
        """Persistent Znuny sync worker - stays alive between cycles."""
        logger.info("Znuny sync worker thread started")
        while self._running:
            self._znuny_sync_event.wait(timeout=5)
            if not self._running:
                break
            if not self._znuny_sync_event.is_set():
                continue
            self._znuny_sync_event.clear()

            try:
                self.run_znuny_sync()
            except Exception as e:
                logger.error(f"Znuny sync failed: {e}")
                db = Database()
                db.log_system("error", "scheduler", f"Znuny sync crashed: {e}")

        logger.info("Znuny sync worker thread stopped")

    def _cleanup_old_logs(self):
        """Delete logs older than 2 days from all log tables."""
        try:
            db = Database()
            results = db.clear_old_logs(days=2)
            total = sum(results.values())
            if total > 0:
                logger.info(f"Cleaned up {total} old log entries: {results}")
        except Exception as e:
            logger.error(f"Log cleanup failed: {e}")

    def _extraction_job(self):
        """Signal the persistent extraction worker to run."""
        if self._extraction_event.is_set():
            logger.debug("Portal extraction still running, skipping this cycle")
            return
        self._extraction_event.set()

    def _znuny_sync_job(self):
        """Signal the persistent Znuny sync worker to run."""
        if self._znuny_sync_event.is_set():
            logger.debug("Znuny sync still running, skipping this cycle")
            return
        self._znuny_sync_event.set()

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

        # Start persistent worker threads
        self._extraction_thread = threading.Thread(
            target=self._extraction_worker_loop,
            daemon=True,
            name="ExtractionWorker"
        )
        self._extraction_thread.start()

        self._znuny_sync_thread = threading.Thread(
            target=self._znuny_sync_worker_loop,
            daemon=True,
            name="ZnunySyncWorker"
        )
        self._znuny_sync_thread.start()

        # Clean up old logs on startup and daily
        self._cleanup_old_logs()
        schedule.every().day.at("00:00").do(self._cleanup_old_logs)

        # Schedule periodic jobs (just signal the persistent workers)
        schedule.every(self._extraction_interval).minutes.do(self._extraction_job)
        schedule.every(self._znuny_sync_interval).minutes.do(self._znuny_sync_job)

        # Run both immediately on start
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
        # Wake up workers so they can exit
        self._extraction_event.set()
        self._znuny_sync_event.set()
        # Clear scheduled jobs to prevent duplicates on restart
        schedule.clear()
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

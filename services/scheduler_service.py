"""
Scheduler Service - Handles background extraction and sync jobs.

This service encapsulates all scheduler-related logic including:
- Running portal extractions (every N minutes, default 5)
- Running Znuny sync (every M minutes, default 1) as a separate job
- Managing the background scheduler thread

Uses persistent worker threads so Playwright browser instances can be
reused across cycles (Playwright objects are bound to their creator thread).
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import psutil
import schedule

from database import Database, now_maldives
from config import Config

MVT = timezone(timedelta(hours=5))
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
        # Per-worker browser reset flags (avoids race where one worker clears before the other sees it)
        self._extraction_reset_requested = threading.Event()
        self._znuny_reset_requested = threading.Event()
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
            if not db.get_portal_enabled(config.name):
                logger.info(f"Portal {config.name} disabled in config, skipping")
                continue

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

    def _is_within_operating_hours(self) -> bool:
        """Check if current MVT time is within configured operating hours."""
        try:
            db = Database()
            hours = db.get_operating_hours()
            if not hours["enabled"]:
                return True
            current_hour = datetime.now(MVT).hour
            return hours["start_hour"] <= current_hour < hours["end_hour"]
        except Exception:
            return True  # Default to running if check fails

    def _is_isp_extraction_enabled(self) -> bool:
        """Check if scheduled ISP portal extraction is enabled (Admin > Config)."""
        try:
            db = Database()
            return db.get_isp_extraction_enabled()
        except Exception:
            return True  # Default to running if check fails

    @staticmethod
    def _reset_thread_playwright():
        """Clear the extraction worker's thread-local Playwright reference.

        The actual processes are already killed by _nuke_all_browsers().
        This just clears the Python reference so a fresh one gets created.
        """
        from utils.browser import BrowserManager
        had_pw = getattr(BrowserManager._thread_local, 'playwright', None) is not None
        BrowserManager._thread_local.__dict__.pop('playwright', None)
        logger.info(f"[reset_thread_pw] thread={threading.current_thread().name}, had_pw={had_pw} — cleared")

    @staticmethod
    def _reset_znuny_playwright():
        """Clear Znuny's shared Playwright references.

        The actual processes are already killed by _nuke_all_browsers().
        This just clears the Python references so fresh ones get created.
        """
        from znuny_client import ZnunyClient
        logger.info(f"[reset_znuny_pw] thread={threading.current_thread().name}, "
                     f"had_pw={ZnunyClient._shared_playwright is not None}, "
                     f"had_page={ZnunyClient._shared_page is not None}")
        ZnunyClient._shared_page = None
        ZnunyClient._shared_context = None
        ZnunyClient._shared_playwright = None
        ZnunyClient._shared_browser_pid = None
        ZnunyClient._shared_logged_in = False
        # Clear caches to free memory (stale after browser kill)
        cache_size = len(getattr(ZnunyClient, '_shared_details_cache', {}))
        ZnunyClient._shared_open_tickets_cache = None
        ZnunyClient._shared_cache_timestamp = None
        ZnunyClient._shared_details_cache = {}
        ZnunyClient._shared_last_login_check = 0
        logger.info(f"Znuny sync worker: references + caches cleared (detail_cache had {cache_size} entries)")

    def _extraction_worker_loop(self):
        """Persistent extraction worker - stays alive between cycles.

        Playwright objects are bound to their creator thread, so keeping
        this thread alive allows browser reuse across extraction cycles.
        """
        logger.info(f"Extraction worker thread started (thread: {threading.current_thread().name})")
        try:
            while self._running:
                self._extraction_event.wait(timeout=5)
                if not self._running:
                    break
                if not self._extraction_event.is_set():
                    continue
                self._extraction_event.clear()
                logger.info("[ExtractionWorker] Woke up - event signaled")

                # If scheduled restart requested a browser reset, clean up
                # this thread's Playwright instance (thread-local, must be reset here).
                if self._extraction_reset_requested.is_set():
                    self._extraction_reset_requested.clear()
                    logger.info("[ExtractionWorker] Reset requested - resetting Playwright")
                    self._reset_thread_playwright()
                    logger.info("[ExtractionWorker] Playwright reset complete")

                if not self._is_within_operating_hours():
                    logger.info("Portal extraction skipped - outside operating hours")
                    continue

                if not self._is_isp_extraction_enabled():
                    logger.info("Portal extraction skipped - ISP checking disabled in config")
                    continue

                logger.info("[ExtractionWorker] Starting portal extraction...")
                try:
                    self.run_portal_extraction()
                    logger.info("[ExtractionWorker] Portal extraction completed successfully")
                except Exception as e:
                    logger.error(f"Portal extraction failed: {e}", exc_info=True)
                    try:
                        db = Database()
                        db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")
                    except Exception:
                        pass
        except Exception as e:
            logger.critical(f"Extraction worker thread DIED: {e}", exc_info=True)
            try:
                db = Database()
                db.log_system("error", "scheduler", f"Extraction worker thread DIED: {e}")
            except Exception:
                pass
        logger.info("Extraction worker thread stopped")

    def _znuny_sync_worker_loop(self):
        """Persistent Znuny sync worker - stays alive between cycles."""
        logger.info(f"Znuny sync worker thread started (thread: {threading.current_thread().name})")
        try:
            while self._running:
                self._znuny_sync_event.wait(timeout=5)
                if not self._running:
                    break
                if not self._znuny_sync_event.is_set():
                    continue
                self._znuny_sync_event.clear()
                logger.info("[ZnunySyncWorker] Woke up - event signaled")

                # If scheduled restart requested a browser reset, clean up
                # Znuny's Playwright state in this thread (must happen in worker thread).
                if self._znuny_reset_requested.is_set():
                    self._znuny_reset_requested.clear()
                    logger.info("[ZnunySyncWorker] Reset requested - resetting Playwright")
                    self._reset_znuny_playwright()
                    logger.info("[ZnunySyncWorker] Playwright reset complete")

                if not self._is_within_operating_hours():
                    logger.info("Znuny sync skipped - outside operating hours")
                    continue

                logger.info("[ZnunySyncWorker] Starting Znuny sync...")
                try:
                    self.run_znuny_sync()
                    logger.info("[ZnunySyncWorker] Znuny sync completed successfully")
                except Exception as e:
                    logger.error(f"Znuny sync failed: {e}", exc_info=True)
                    try:
                        db = Database()
                        db.log_system("error", "scheduler", f"Znuny sync crashed: {e}")
                    except Exception:
                        pass
        except Exception as e:
            logger.critical(f"Znuny sync worker thread DIED: {e}", exc_info=True)
            try:
                db = Database()
                db.log_system("error", "scheduler", f"Znuny sync worker thread DIED: {e}")
            except Exception:
                pass
        logger.info("Znuny sync worker thread stopped")

    def _generate_customer_report(self):
        """Trigger a (background) regeneration of the customer performance report."""
        try:
            from services.customer_report_service import CustomerReportService
            if CustomerReportService.start_generation():
                logger.info("Customer report generation triggered")
            else:
                logger.info("Customer report already generating - skipped")
        except Exception as e:
            logger.error(f"Failed to trigger customer report: {e}")

    def _quick_isp_check(self):
        """Every 1 min: link unlinked active ISP tickets to Znuny and refresh the
        latest Znuny comment for active linked ISP tickets. Runs in a short-lived
        thread so it never blocks the scheduler loop, with a reentrancy guard."""
        if not self._has_db_credentials() or not self._is_within_operating_hours():
            return
        if getattr(self, "_quick_check_running", False):
            logger.debug("Quick ISP check still running, skipping this minute")
            return
        self._quick_check_running = True

        def _run():
            try:
                ZnunyService().quick_isp_znuny_check()
            except Exception as e:
                logger.error(f"Quick ISP check failed: {e}")
            finally:
                self._quick_check_running = False

        threading.Thread(target=_run, daemon=True, name="QuickISPCheck").start()

    def _has_nocbot_credentials(self) -> bool:
        """Check if the NocBot API URL + key are configured."""
        db = Database()
        config = db.get_config_settings()
        return bool(config.get("NOCBOT_URL") and config.get("NOCBOT_API_KEY"))

    def _check_ont_status(self):
        """Every 5 min: check whether active ISP tickets' addresses have an ONT
        in SMX via NocBot. Never-checked tickets are checked promptly; tickets
        already confirmed 'not found' are retried after an hour (enforced by
        get_tickets_needing_ont_check's cutoff), gated to operating hours like
        the other periodic checks. Runs in a short-lived thread so it never
        blocks the scheduler loop, with a reentrancy guard."""
        if not self._has_nocbot_credentials() or not self._is_within_operating_hours():
            return
        if getattr(self, "_ont_check_running", False):
            logger.debug("ONT check still running, skipping this cycle")
            return
        self._ont_check_running = True

        def _run():
            try:
                from services.nocbot_service import NocBotService
                NocBotService().check_pending_tickets()
            except Exception as e:
                logger.error(f"ONT status check failed: {e}")
            finally:
                self._ont_check_running = False

        threading.Thread(target=_run, daemon=True, name="OntStatusCheck").start()

    def _generate_staff_snapshots(self):
        """Freeze the just-completed day's per-staff stats snapshot (daily, at
        midnight). Today is served live by the page, so only prior days need a
        stored snapshot. Generates yesterday and (idempotently) today."""
        try:
            db = Database()
            from datetime import timedelta as _td
            yesterday = (now_maldives().date() - _td(days=1)).isoformat()
            today = now_maldives().date().isoformat()
            c1 = db.generate_staff_daily_snapshot(yesterday)
            db.generate_staff_daily_snapshot(today)
            logger.info(f"Staff snapshot frozen for {yesterday}: {c1} staff")
        except Exception as e:
            logger.error(f"Staff snapshot generation failed: {e}")

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

    def _check_container_restart(self):
        """Daily container restart (Admin > Config). Reads settings from the DB on
        every tick (like operating hours) so a changed hour takes effect without
        re-registering a schedule job. Fires once per calendar day (MVT) at or after
        the configured hour, then exits the process — the `restart: unless-stopped`
        Docker policy brings the container straight back up.

        The "already fired today" guard is persisted in the DB (``container_restart_last_date``),
        not process memory: an in-memory flag resets to None on every new process, so the
        replacement container would see the same trigger hour and immediately restart again,
        looping for the rest of that hour. Reading it back from the DB after the restart makes
        the guard survive the process it triggers.

        Using `>=` instead of `==` on the hour also means a missed window (e.g. the app was down
        or the check didn't run during the configured hour) is caught up and fires once as soon
        as the check next runs that day, rather than waiting until the same hour next day.
        """
        try:
            db = Database()
            settings = db.get_container_restart_settings()
            if not settings["enabled"]:
                return
            now = now_maldives()
            today_str = now.date().isoformat()
            if settings["last_restart_date"] == today_str:
                return  # already fired today
            if now.hour < settings["hour"]:
                return  # scheduled hour hasn't arrived yet today

            db.mark_container_restarted(today_str)

            logger.warning(f"Daily container restart triggered ({settings['hour']}:00 MVT) - exiting process")
            db.log_system("warning", "scheduler", f"Daily container restart triggered ({settings['hour']}:00 MVT)")

            # Best-effort cleanup so the browser processes don't linger as zombies
            # under the container's PID 1 during the brief window before exit.
            try:
                self._nuke_all_browsers()
            except Exception:
                pass

            os._exit(0)
        except Exception as e:
            logger.error(f"Container restart check failed: {e}")

    @staticmethod
    def _nuke_all_browsers():
        """Kill ALL chromium and playwright node processes system-wide, then clear all references.

        This is a simple, reliable nuclear approach:
        1. Find and kill every chromium/node process (no PID tracking needed)
        2. Clear all Python-side references
        3. Workers will recreate everything from scratch on next cycle
        """
        killed_pids = []

        # Kill all chromium and node (playwright driver) processes
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                pname = (proc.info['name'] or '').lower()
                if 'chromium' in pname or 'chrome' in pname:
                    proc.kill()
                    killed_pids.append(f"chromium(PID {proc.info['pid']})")
                elif 'node' in pname:
                    # Playwright driver is a node process
                    try:
                        cmdline = ' '.join(proc.cmdline()).lower()
                        if 'playwright' in cmdline:
                            proc.kill()
                            killed_pids.append(f"playwright-node(PID {proc.info['pid']})")
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Clear all Python-side references
        from extractors.base import BaseExtractor
        from znuny_client import ZnunyClient
        from utils.browser import BrowserManager

        # Clear portal browser references (thread-safe)
        with BaseExtractor._portal_browsers_lock:
            portals_cleared = list(BaseExtractor._portal_browsers.keys())
            BaseExtractor._portal_browsers.clear()

        # Clear extraction worker's thread-local Playwright
        # (this only works if called from the same thread, but we clear it anyway)
        BrowserManager._thread_local.__dict__.pop('playwright', None)

        # Clear Znuny shared state
        ZnunyClient._shared_page = None
        ZnunyClient._shared_context = None
        ZnunyClient._shared_playwright = None
        ZnunyClient._shared_browser_pid = None
        ZnunyClient._shared_logged_in = False

        logger.info(f"[nuke] Killed processes: {killed_pids or 'none'}. "
                     f"Cleared portal refs: {portals_cleared or 'none'}")
        return killed_pids

    # Restart browsers only when extraction has been stale this long (minutes).
    STALE_RESTART_MINUTES = 15

    def _check_browser_staleness(self):
        """Restart browsers only if extraction has been stale for >=15 minutes.

        This is the ONLY thing that proactively restarts browsers. A healthy
        extraction worker logs every portal each cycle (default 5 min), so a
        hung browser blocks the cycle and lets every portal's last extraction
        age past the threshold. We do NOT restart on a fixed timer, on memory
        leaks, or for any other reason here.
        """
        try:
            db = Database()

            # No extraction is expected in these states, so an old timestamp is
            # not "stale" — never restart because of them.
            if not self._has_db_credentials():
                return
            if not self._is_within_operating_hours():
                return
            if not self._is_isp_extraction_enabled():
                return

            from extractors.base import BaseExtractor
            paused = set(BaseExtractor.manual_cf_paused_portals())

            last_per_portal = db.get_last_extraction_per_portal()
            # Consider only portals we actually expect to be extracting now.
            candidates = {
                p: info for p, info in last_per_portal.items()
                if p.lower() not in {q.lower() for q in paused}
            }
            if not candidates:
                return  # nothing extracting yet (startup) or all paused

            now = now_maldives()
            # The worker runs all portals sequentially, so the freshest log
            # reflects the pipeline as a whole. If even that is old, it's hung.
            freshest_age_min = None
            for info in candidates.values():
                ts = self._parse_extracted_at(info.get("extracted_at"))
                if ts is None:
                    continue
                age_min = (now - ts).total_seconds() / 60.0
                if freshest_age_min is None or age_min < freshest_age_min:
                    freshest_age_min = age_min

            if freshest_age_min is None:
                return  # couldn't parse any timestamp; don't restart blindly
            if freshest_age_min < self.STALE_RESTART_MINUTES:
                return  # extraction is fresh — nothing to do

            logger.warning(
                f"Extraction stale for {freshest_age_min:.0f} min "
                f"(>= {self.STALE_RESTART_MINUTES}) - nuking all browsers"
            )
            db.log_system(
                "warning", "scheduler",
                f"Extraction stale {freshest_age_min:.0f} min - restarting browsers"
            )

            killed = self._nuke_all_browsers()

            # Signal workers to reset their thread-local Playwright instances
            self._extraction_reset_requested.set()
            self._znuny_reset_requested.set()

            # Trigger immediate re-extraction and sync
            self._extraction_event.set()
            self._znuny_sync_event.set()

            msg = f"Killed: {', '.join(killed)}" if killed else "No active browsers found"
            logger.info(f"Stale browser restart complete - {msg}")
            db.log_system("info", "scheduler", f"Browser restart complete - {msg}")
        except Exception as e:
            logger.error(f"Browser staleness check failed: {e}")

    @staticmethod
    def _parse_extracted_at(value) -> Optional[datetime]:
        """Parse an extraction_logs.extracted_at value into an MVT-aware datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            try:
                dt = datetime.fromisoformat(str(value).strip())
            except ValueError:
                return None
        # Stored as MVT (now_maldives); assume MVT if no tzinfo present.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MVT)
        return dt

    def _check_and_restart_workers(self):
        """Check if worker threads are alive and restart dead ones."""
        ext_alive = self._extraction_thread.is_alive() if self._extraction_thread else False
        znuny_alive = self._znuny_sync_thread.is_alive() if self._znuny_sync_thread else False
        logger.info(f"[health] ExtractionWorker alive={ext_alive}, ZnunySyncWorker alive={znuny_alive}")

        restarted = []
        if self._extraction_thread and not self._extraction_thread.is_alive():
            logger.error("Extraction worker thread is DEAD - restarting")
            self._extraction_thread = threading.Thread(
                target=self._extraction_worker_loop,
                daemon=True,
                name="ExtractionWorker"
            )
            self._extraction_thread.start()
            restarted.append("ExtractionWorker")

        if self._znuny_sync_thread and not self._znuny_sync_thread.is_alive():
            logger.error("Znuny sync worker thread is DEAD - restarting")
            self._znuny_sync_thread = threading.Thread(
                target=self._znuny_sync_worker_loop,
                daemon=True,
                name="ZnunySyncWorker"
            )
            self._znuny_sync_thread.start()
            restarted.append("ZnunySyncWorker")

        if restarted:
            db = Database()
            db.log_system("error", "scheduler", f"Restarted dead worker threads: {', '.join(restarted)}")
            # Trigger immediate run
            self._extraction_event.set()
            self._znuny_sync_event.set()

    def _worker_health_check(self):
        """Check if worker threads are alive and restart dead ones."""
        self._check_and_restart_workers()

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

        # Customer performance report: regenerate daily, and once on startup if missing
        schedule.every().day.at("01:00").do(self._generate_customer_report)
        try:
            from services.customer_report_service import CustomerReportService
            if not CustomerReportService.report_exists():
                self._generate_customer_report()
        except Exception as e:
            logger.error(f"Startup customer report check failed: {e}")

        # Schedule periodic jobs (just signal the persistent workers)
        schedule.every(self._extraction_interval).minutes.do(self._extraction_job)
        schedule.every(self._znuny_sync_interval).minutes.do(self._znuny_sync_job)
        schedule.every(5).minutes.do(self._worker_health_check)
        # Restart browsers ONLY when extraction has been stale for >=15 min.
        schedule.every(5).minutes.do(self._check_browser_staleness)
        # Every 1 min: link active ISP tickets to Znuny + refresh their last comment
        schedule.every(1).minutes.do(self._quick_isp_check)
        # Every 5 min: check active ISP tickets' addresses for an ONT in SMX (NocBot)
        schedule.every(5).minutes.do(self._check_ont_status)
        # Freeze the completed day's per-staff snapshot once at midnight (today is
        # served live by the page; the creator sweep refreshes prior snapshots).
        schedule.every().day.at("00:30").do(self._generate_staff_snapshots)
        # Daily container restart (Admin > Config, disabled by default) - checked
        # every minute so a changed restart hour takes effect immediately.
        schedule.every(1).minutes.do(self._check_container_restart)

        # Run both immediately on start
        self._extraction_job()
        self._znuny_sync_job()
        # Ensure yesterday's snapshot exists on startup (covers midnight downtime)
        self._generate_staff_snapshots()

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
        # Wait for worker threads to finish
        if self._extraction_thread and self._extraction_thread.is_alive():
            self._extraction_thread.join(timeout=10)
        if self._znuny_sync_thread and self._znuny_sync_thread.is_alive():
            self._znuny_sync_thread.join(timeout=10)
        # Clear scheduled jobs to prevent duplicates on restart
        schedule.clear()
        logger.info("Scheduler stop requested")

    def get_status(self) -> dict:
        """Get scheduler status."""
        status = {
            "running": self.is_running,
            "extraction_interval_minutes": self._extraction_interval,
            "znuny_sync_interval_minutes": self._znuny_sync_interval,
            "thread_alive": self._thread.is_alive() if self._thread else False
        }
        try:
            db = Database()
            hours = db.get_operating_hours()
            status["operating_hours"] = hours
            status["within_operating_hours"] = self._is_within_operating_hours()
            status["isp_extraction_enabled"] = db.get_isp_extraction_enabled()
            status["container_restart"] = db.get_container_restart_settings()
            status["portals_enabled"] = {
                p.name: db.get_portal_enabled(p.name) for p in Config.get_all_portals()
            }
            # Portals paused awaiting a manual Cloudflare bypass via noVNC.
            from extractors.base import BaseExtractor
            status["manual_cf_paused"] = BaseExtractor.manual_cf_paused_portals()
        except Exception:
            pass
        return status


# Global scheduler instance
_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
    return _scheduler

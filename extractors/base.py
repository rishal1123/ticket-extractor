from abc import ABC, abstractmethod
from typing import Optional
import os
import shutil
import time
import psutil

from playwright.sync_api import Error as PlaywrightError

from config import PortalConfig
from models.ticket import Ticket
from database import Database
from utils.browser import BrowserManager
from utils.logger import get_logger

# Default memory threshold in MB - subclasses can override via MEMORY_LIMIT_MB
DEFAULT_MEMORY_LIMIT_MB = 800
# Directory for persistent browser sessions
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_sessions")


class BaseExtractor(ABC):
    """Base class for all portal extractors."""

    # Per-portal browser instances (each portal gets its own Chromium)
    _portal_browsers: dict[str, BrowserManager] = {}
    # Track consecutive 0-ticket extraction cycles per portal
    _consecutive_zero_counts: dict = {}
    # Memory limit per browser - override in subclass for heavier portals
    MEMORY_LIMIT_MB = DEFAULT_MEMORY_LIMIT_MB

    def __init__(self, config: PortalConfig, db: Database, headless: bool = False):
        self.config = config
        self.db = db
        self.headless = headless
        self.browser: Optional[BrowserManager] = None
        self.logger = get_logger(f"extractor.{config.name}")

    @abstractmethod
    def login(self) -> bool:
        """
        Login to the portal.
        Returns True if login successful, False otherwise.
        """
        pass

    @abstractmethod
    def extract_tickets(self) -> list[Ticket]:
        """
        Extract all tickets from the portal.
        Returns list of Ticket objects.
        """
        pass

    @abstractmethod
    def logout(self):
        """Logout from the portal."""
        pass

    def is_logged_in(self) -> bool:
        """
        Check if currently logged in.
        Override in subclass to implement portal-specific check.
        """
        return False

    def fetch_completion_notes(self, missing_ticket_ids: set[str]) -> dict[str, str]:
        """Fetch final notes/comments for tickets about to be marked complete.

        Override in portal-specific extractors that support this.
        Default implementation returns empty dict (no notes fetched).

        Args:
            missing_ticket_ids: Set of ticket_ids that disappeared from the portal

        Returns:
            Dict mapping ticket_id -> notes text
        """
        return {}

    def ensure_logged_in(self) -> bool:
        """Ensure we are logged in, re-login if session expired."""
        if self.is_logged_in():
            self.logger.info("Session still active, skipping login")
            self.db.log_login_event(self.config.name, "session_reused")
            return True
        self.logger.info("Session expired or not logged in, logging in...")
        self.db.log_login_event(self.config.name, "login_attempt")
        success = self.login()
        if success:
            self.db.log_login_event(self.config.name, "login_success")
        else:
            self.db.log_login_event(self.config.name, "login_failed", success=False)
            self.db.log_system("error", f"extractor.{self.config.name}", "Login failed")
        return success

    def _get_browser_memory_mb(self, browser: BrowserManager) -> float:
        """Get total memory usage (MB) for the browser's Chromium process tree."""
        if not browser:
            return 0.0
        pid = browser.get_browser_pid()
        if not pid:
            return 0.0
        try:
            parent = psutil.Process(pid)
            total = parent.memory_info().rss
            for child in parent.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return total / (1024 * 1024)
        except Exception:
            return 0.0

    def _check_memory_and_reset(self, portal: str, browser: BrowserManager) -> Optional[BrowserManager]:
        """Check browser memory usage and reset if over limit. Returns None if reset needed."""
        mem_mb = self._get_browser_memory_mb(browser)
        if mem_mb <= 0:
            return browser
        self.logger.info(f"[{portal}] Browser memory: {mem_mb:.0f} MB (limit: {self.MEMORY_LIMIT_MB} MB)")
        if mem_mb > self.MEMORY_LIMIT_MB:
            self.logger.warning(f"[{portal}] Memory {mem_mb:.0f} MB exceeds {self.MEMORY_LIMIT_MB} MB - resetting browser")
            self.db.log_system(
                "warning", f"extractor.{portal}",
                f"Browser memory {mem_mb:.0f} MB exceeds {self.MEMORY_LIMIT_MB} MB limit - resetting"
            )
            try:
                browser.stop()
            except Exception:
                pass
            BaseExtractor._portal_browsers.pop(portal, None)
            return None
        return browser

    def _get_session_dir(self) -> str:
        """Get persistent session directory for this portal."""
        session_dir = os.path.join(SESSIONS_DIR, self.config.name.lower())
        os.makedirs(session_dir, exist_ok=True)
        return session_dir

    def _clear_session_dir(self):
        """Delete persistent session directory to force a fresh browser next cycle."""
        session_dir = os.path.join(SESSIONS_DIR, self.config.name.lower())
        if os.path.exists(session_dir):
            try:
                shutil.rmtree(session_dir)
                self.logger.info(f"Cleared browser session directory: {session_dir}")
            except Exception as e:
                self.logger.warning(f"Failed to clear session directory: {e}")

    def _get_or_create_browser(self) -> BrowserManager:
        """Get or create a dedicated browser for this portal.

        Uses Playwright persistent context so login sessions survive browser resets.
        """
        portal = self.config.name
        existing = BaseExtractor._portal_browsers.get(portal)

        if existing is not None:
            if existing.is_alive():
                # Check memory before reusing
                checked = self._check_memory_and_reset(portal, existing)
                if checked is not None:
                    self.logger.info(f"[{portal}] Reusing dedicated browser")
                    return checked
                # Memory too high - session data persists on disk, safe to recreate
                self.logger.info(f"[{portal}] Memory limit exceeded, recreating (session persisted)")
            else:
                self.logger.info(f"[{portal}] Browser died, recreating (session persisted)")
                self.db.log_system("warning", f"extractor.{portal}", "Browser died, recreating with persisted session")
                try:
                    existing.stop()
                except Exception:
                    pass
                BaseExtractor._portal_browsers.pop(portal, None)

        browser = BrowserManager(headless=self.headless)
        session_dir = self._get_session_dir()
        browser.start(user_data_dir=session_dir)
        BaseExtractor._portal_browsers[portal] = browser
        self.logger.info(f"[{portal}] Created persistent browser (session dir: {session_dir})")
        return browser

    def run(self, max_retries: int = 3, keep_session: bool = True) -> dict:
        """
        Run the extraction process.
        Returns extraction results.
        """
        result = {
            "portal": self.config.name,
            "status": "failed",
            "tickets_found": 0,
            "tickets_new": 0,
            "tickets_updated": 0,
            "tickets_completed": 0,
            "error": None
        }

        for attempt in range(max_retries):
            try:
                self.logger.info(f"Starting extraction (attempt {attempt + 1}/{max_retries})")

                # Get current active ticket IDs before extraction (for completion tracking)
                existing_ticket_ids = self.db.get_active_ticket_ids(self.config.name)

                # Get or create browser (reuses existing session if available)
                self.browser = self._get_or_create_browser()

                # Ensure logged in (checks session, re-logins if needed)
                if not self.ensure_logged_in():
                    raise Exception("Login failed")

                # Wait for page to stabilize
                time.sleep(2)

                # Extract tickets
                tickets = self.extract_tickets()
                result["tickets_found"] = len(tickets)
                self.logger.info(f"Found {len(tickets)} tickets")

                # Track found ticket IDs
                found_ticket_ids = set()

                # Save tickets to database
                for ticket in tickets:
                    found_ticket_ids.add(ticket.ticket_id)
                    ticket_id, is_new, is_updated = self.db.upsert_ticket(ticket)
                    if is_new:
                        result["tickets_new"] += 1
                    elif is_updated:
                        result["tickets_updated"] += 1

                # Mark missing tickets as complete
                portal_name = self.config.name
                if found_ticket_ids:
                    # Tickets found - reset consecutive zero counter
                    BaseExtractor._consecutive_zero_counts[portal_name] = 0
                    missing_ticket_ids = existing_ticket_ids - found_ticket_ids
                    if missing_ticket_ids:
                        # Fetch final notes before marking complete
                        try:
                            completion_notes = self.fetch_completion_notes(missing_ticket_ids)
                            if completion_notes:
                                self.db.update_ticket_notes_bulk(self.config.name, completion_notes)
                                self.logger.info(f"Updated notes for {len(completion_notes)} tickets before completion")
                        except Exception as e:
                            self.logger.warning(f"Error fetching completion notes (non-critical): {e}")

                        completed_count = self.db.mark_tickets_complete(
                            self.config.name, list(missing_ticket_ids)
                        )
                        result["tickets_completed"] = completed_count
                        self.logger.info(f"Marked {completed_count} tickets as complete (disappeared from portal)")
                elif existing_ticket_ids:
                    # 0 tickets found but active tickets exist
                    # Track consecutive zero-ticket cycles to avoid false completions
                    zero_count = BaseExtractor._consecutive_zero_counts.get(portal_name, 0) + 1
                    BaseExtractor._consecutive_zero_counts[portal_name] = zero_count
                    required_cycles = 3

                    if zero_count >= required_cycles:
                        # 3+ consecutive cycles with 0 tickets - safe to mark complete
                        # Fetch final notes before marking complete
                        try:
                            completion_notes = self.fetch_completion_notes(existing_ticket_ids)
                            if completion_notes:
                                self.db.update_ticket_notes_bulk(self.config.name, completion_notes)
                                self.logger.info(f"Updated notes for {len(completion_notes)} tickets before completion")
                        except Exception as e:
                            self.logger.warning(f"Error fetching completion notes (non-critical): {e}")

                        completed_count = self.db.mark_tickets_complete(
                            self.config.name, list(existing_ticket_ids)
                        )
                        result["tickets_completed"] = completed_count
                        BaseExtractor._consecutive_zero_counts[portal_name] = 0
                        self.logger.info(
                            f"Marked {completed_count} tickets as complete after "
                            f"{zero_count} consecutive zero-ticket cycles"
                        )
                    else:
                        self.logger.warning(
                            f"Extraction returned 0 tickets but {len(existing_ticket_ids)} active - "
                            f"zero-ticket cycle {zero_count}/{required_cycles}, skipping completion"
                        )

                # Don't logout if keeping session active
                if not keep_session:
                    try:
                        self.logout()
                    except Exception as e:
                        self.logger.warning(f"Logout error (non-critical): {e}")

                result["status"] = "success"
                # Log memory usage after successful extraction
                mem_mb = self._get_browser_memory_mb(self.browser)
                if mem_mb > 0:
                    result["memory_mb"] = round(mem_mb, 1)
                self.logger.info(
                    f"Extraction complete: {result['tickets_found']} found, "
                    f"{result['tickets_new']} new, {result['tickets_updated']} updated, "
                    f"{result['tickets_completed']} completed"
                    f"{f', memory: {mem_mb:.0f}MB' if mem_mb > 0 else ''}"
                )
                break

            except Exception as e:
                self.logger.error(f"Extraction failed: {e}")
                result["error"] = str(e)
                self.db.log_system(
                    "error", f"extractor.{self.config.name}",
                    f"Extraction attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                # Kill this portal's browser on error so next attempt starts fresh
                portal = self.config.name
                existing = BaseExtractor._portal_browsers.get(portal)
                if existing is not None:
                    try:
                        existing.stop()
                    except Exception:
                        pass
                    BaseExtractor._portal_browsers.pop(portal, None)
                self.browser = None
                if attempt < max_retries - 1:
                    self.logger.info(f"Retrying in 5 seconds...")
                    time.sleep(5)

        # If all retries failed, clear session data for a fresh start next cycle
        if result["status"] == "failed":
            self.logger.warning(
                f"All {max_retries} attempts failed - clearing browser session for fresh start"
            )
            self._clear_session_dir()
            self.db.log_system(
                "warning", f"extractor.{self.config.name}",
                f"All {max_retries} extraction attempts failed - browser session cleared"
            )

        # Log extraction result
        self.db.log_extraction(
            portal=result["portal"],
            status=result["status"],
            tickets_found=result["tickets_found"],
            tickets_new=result["tickets_new"],
            tickets_updated=result["tickets_updated"],
            tickets_completed=result["tickets_completed"],
            error_message=result["error"]
        )

        return result

    def navigate_to(self, url: str, timeout: int = None, wait_until: str = None):
        """Navigate to a URL. timeout is in milliseconds (default uses page default)."""
        self.logger.debug(f"Navigating to: {url}")
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if wait_until is not None:
            kwargs["wait_until"] = wait_until
        self.browser.page.goto(url, **kwargs)

    def wait_and_click(self, selector: str, timeout: int = 10) -> bool:
        """Wait for element and click it."""
        return self.browser.safe_click(selector, timeout)

    def wait_and_type(self, selector: str, text: str, timeout: int = 10) -> bool:
        """Wait for element and type text."""
        return self.browser.safe_send_keys(selector, text, timeout)

    def get_element_text(self, selector: str, timeout: int = 10) -> Optional[str]:
        """Get text from element."""
        return self.browser.get_text(selector, timeout)

    def find_elements(self, selector: str) -> list:
        """Find multiple elements."""
        try:
            return self.browser.page.query_selector_all(selector)
        except Exception as e:
            self.logger.warning(f"Error finding elements {selector}: {e}")
            return []

    def take_screenshot(self, name: str):
        """Take screenshot for debugging."""
        if self.browser:
            filename = f"screenshots/{self.config.name}_{name}_{int(time.time())}.png"
            self.browser.take_screenshot(filename)

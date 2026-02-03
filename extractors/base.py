from abc import ABC, abstractmethod
from typing import Optional
import time

from selenium.webdriver.common.by import By

from config import PortalConfig
from models.ticket import Ticket
from database import Database
from utils.browser import BrowserManager
from utils.logger import get_logger


class BaseExtractor(ABC):
    """Base class for all portal extractors."""

    # Class-level browser instances for session persistence
    _browsers: dict = {}

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

    def ensure_logged_in(self) -> bool:
        """Ensure we are logged in, re-login if session expired."""
        if self.is_logged_in():
            self.logger.info("Session still active, skipping login")
            return True
        self.logger.info("Session expired or not logged in, logging in...")
        return self.login()

    def _get_or_create_browser(self) -> BrowserManager:
        """Get existing browser or create new one for session persistence."""
        portal_name = self.config.name
        if portal_name in BaseExtractor._browsers:
            browser = BaseExtractor._browsers[portal_name]
            # Check if browser is still alive
            try:
                browser.driver.current_url
                self.logger.info("Reusing existing browser session")
                return browser
            except:
                self.logger.info("Browser session died, creating new one")
                del BaseExtractor._browsers[portal_name]

        # Create new browser
        browser = BrowserManager(headless=self.headless)
        browser.start()
        BaseExtractor._browsers[portal_name] = browser
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
                missing_ticket_ids = existing_ticket_ids - found_ticket_ids
                if missing_ticket_ids:
                    completed_count = self.db.mark_tickets_complete(
                        self.config.name, list(missing_ticket_ids)
                    )
                    result["tickets_completed"] = completed_count
                    self.logger.info(f"Marked {completed_count} tickets as complete (disappeared from portal)")

                # Don't logout if keeping session active
                if not keep_session:
                    try:
                        self.logout()
                    except Exception as e:
                        self.logger.warning(f"Logout error (non-critical): {e}")

                result["status"] = "success"
                self.logger.info(
                    f"Extraction complete: {result['tickets_found']} found, "
                    f"{result['tickets_new']} new, {result['tickets_updated']} updated, "
                    f"{result['tickets_completed']} completed"
                )
                break

            except Exception as e:
                self.logger.error(f"Extraction failed: {e}")
                result["error"] = str(e)
                # Kill browser on error so next attempt starts fresh
                if self.config.name in BaseExtractor._browsers:
                    try:
                        BaseExtractor._browsers[self.config.name].stop()
                    except:
                        pass
                    del BaseExtractor._browsers[self.config.name]
                self.browser = None
                if attempt < max_retries - 1:
                    self.logger.info(f"Retrying in 5 seconds...")
                    time.sleep(5)

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

    def navigate_to(self, url: str):
        """Navigate to a URL."""
        self.logger.debug(f"Navigating to: {url}")
        self.browser.driver.get(url)

    def wait_and_click(self, by: By, value: str, timeout: int = 10) -> bool:
        """Wait for element and click it."""
        return self.browser.safe_click(by, value, timeout)

    def wait_and_type(self, by: By, value: str, text: str, timeout: int = 10) -> bool:
        """Wait for element and type text."""
        return self.browser.safe_send_keys(by, value, text, timeout)

    def get_element_text(self, by: By, value: str, timeout: int = 10) -> Optional[str]:
        """Get text from element."""
        return self.browser.get_text(by, value, timeout)

    def find_elements(self, by: By, value: str) -> list:
        """Find multiple elements."""
        try:
            return self.browser.driver.find_elements(by, value)
        except Exception as e:
            self.logger.warning(f"Error finding elements {by}={value}: {e}")
            return []

    def take_screenshot(self, name: str):
        """Take screenshot for debugging."""
        if self.browser:
            filename = f"screenshots/{self.config.name}_{name}_{int(time.time())}.png"
            self.browser.take_screenshot(filename)

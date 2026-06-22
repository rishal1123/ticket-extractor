import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .base import BaseExtractor
from models.ticket import Ticket


class ROLExtractor(BaseExtractor):
    """Extractor for ROL portal (Kayako helpdesk system)."""

    # ============================================================
    # URL Configuration
    # ============================================================
    # Filter URLs - HDC department (55)
    # Status codes: 4=Open, 5=On Hold, 6=Closed, 7=Today
    OPEN_TICKETS_URL = "https://support.rol.net.mv/staff/index.php?/Tickets/Manage/Filter/55/4/-1"

    # Navigation timeout for ROL portal (ms) - ROL can be slow
    NAV_TIMEOUT_MS = 30000

    # ============================================================
    # CSS SELECTORS
    # ============================================================

    # Login page selectors
    LOGIN_USERNAME_SELECTOR = "input[name='username']"
    LOGIN_PASSWORD_SELECTOR = "input[name='password']"
    LOGIN_BUTTON_SELECTOR = "input[type='submit']"

    # Grid selectors
    GRID_CONTAINER_SELECTOR = ".gridcontents_ticketmanagegrid_parent"
    GRID_ROW_SELECTOR = "tr[id^='gridrowid_ticketmanagegrid_']"

    # Ticket detail page selectors
    TICKET_GENERAL_CONTAINER_SELECTOR = ".ticketgeneralcontainer"
    TICKET_POST_CONTAINER_SELECTOR = ".ticketpostcontainer"
    TICKET_INFO_SELECTOR = ".ticketinfoitem"

    # Logout
    LOGOUT_SELECTOR = "[onclick*='Logout']"

    # ============================================================

    def login(self) -> bool:
        """Login to ROL portal."""
        self.logger.info(f"Logging into ROL portal: {self.config.url}")

        try:
            self.browser.page.goto(self.config.url, timeout=self.NAV_TIMEOUT_MS)
            time.sleep(2)

            # Check if already logged in
            if self.is_logged_in():
                self.logger.info("Already logged in")
                return True

            # Wait for login form to be ready
            try:
                self.browser.page.wait_for_selector(
                    self.LOGIN_USERNAME_SELECTOR, timeout=15000, state="visible"
                )
            except PlaywrightTimeout:
                self.logger.error("Login form did not appear within 15s")
                return False

            # Enter username
            if not self.wait_and_type(self.LOGIN_USERNAME_SELECTOR, self.config.username):
                self.logger.error("Failed to enter username")
                return False

            # Enter password
            if not self.wait_and_type(self.LOGIN_PASSWORD_SELECTOR, self.config.password):
                self.logger.error("Failed to enter password")
                return False

            # Click login button and wait for navigation to complete
            try:
                with self.browser.page.expect_navigation(
                    timeout=self.NAV_TIMEOUT_MS, wait_until="domcontentloaded"
                ):
                    if not self.wait_and_click(self.LOGIN_BUTTON_SELECTOR):
                        self.logger.error("Failed to click login button")
                        return False
            except PlaywrightTimeout:
                self.logger.warning("Navigation after login click timed out, checking URL anyway")

            # Wait briefly for any redirects to settle
            time.sleep(2)

            # Verify login success: check URL is no longer the login page
            current_url = self.browser.page.url.lower()
            if "/login" in current_url or current_url.rstrip("/") == self.config.url.lower().rstrip("/"):
                # Double-check by looking for the logout button (more reliable)
                if not self.is_logged_in():
                    self.logger.error("Login failed - still on login page")
                    self.take_screenshot("login_failed")
                    return False

            # Final confirmation: wait for a post-login element to appear
            try:
                self.browser.page.wait_for_selector(
                    self.LOGOUT_SELECTOR, timeout=15000, state="attached"
                )
            except PlaywrightTimeout:
                self.logger.error("Login failed - logout button never appeared")
                self.take_screenshot("login_no_logout_btn")
                return False

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            self.take_screenshot("login_error")
            return False

    def detail_url(self, ticket) -> str:
        # ROL stores the internal id in ticket_id; the display id is in account.
        return f"https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{ticket.ticket_id}/inbox/55/-1/-1"

    def is_logged_in(self) -> bool:
        """Check if currently logged in by looking for logout button."""
        try:
            # Use wait_for_selector with a short timeout instead of instant query
            # This handles cases where the page is still loading
            element = self.browser.page.wait_for_selector(
                self.LOGOUT_SELECTOR, timeout=3000, state="attached"
            )
            return element is not None
        except PlaywrightTimeout:
            return False
        except Exception:
            return False

    def extract_tickets(self) -> list[Ticket]:
        """Extract tickets from the ROL grid."""
        tickets = []

        try:
            # Navigate to Open tickets page
            self.logger.info("Navigating to Open tickets")
            self.browser.page.goto(self.OPEN_TICKETS_URL, timeout=self.NAV_TIMEOUT_MS)
            time.sleep(3)

            # Wait for grid to load
            self.browser.page.wait_for_selector(self.GRID_CONTAINER_SELECTOR, timeout=self.NAV_TIMEOUT_MS)

            # Extract tickets from grid
            tickets = self._extract_tickets_from_grid()
            self.logger.info(f"Extracted {len(tickets)} tickets from grid")

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return tickets

    def _extract_tickets_from_grid(self) -> list[Ticket]:
        """Extract tickets from the grid rows."""
        tickets = []

        try:
            rows = self.browser.page.query_selector_all(self.GRID_ROW_SELECTOR)
            self.logger.info(f"Found {len(rows)} ticket rows")

            for i, row in enumerate(rows):
                try:
                    ticket = self._parse_grid_row(row, i)
                    if ticket:
                        tickets.append(ticket)
                except Exception as e:
                    self.logger.warning(f"Error parsing row {i}: {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error extracting from grid: {e}")

        return tickets

    def _parse_grid_row(self, row, index: int) -> Ticket | None:
        """Parse a grid row to extract ticket data."""
        try:
            # Get row ID to extract ticket internal ID
            row_id = row.get_attribute('id') or ""
            # Format: gridrowid_ticketmanagegrid_115385
            internal_id = row_id.split('_')[-1] if row_id else None

            # Get all cells
            cells = row.query_selector_all("td")

            if len(cells) < 14:
                self.logger.warning(f"Row {index} has insufficient cells: {len(cells)}")
                return None

            # Extract data from cells (0-indexed)
            # Cell 5 = Date
            date_text = (cells[5].text_content() or "").strip() if len(cells) > 5 else ""

            # Cell 6 = Display ID (ROL250141)
            display_id = ""
            try:
                link = cells[6].query_selector("a")
                if link:
                    display_id = (link.text_content() or "").strip()
            except Exception:
                pass
            if not display_id:
                display_id = (cells[6].text_content() or "").strip() if len(cells) > 6 else ""

            if not display_id:
                self.logger.warning(f"Row {index} has no display ID")
                return None

            # Cell 7 = Owner/Customer name
            customer_name = (cells[7].text_content() or "").strip() if len(cells) > 7 else ""

            # Cell 8 = Priority
            priority = (cells[8].text_content() or "").strip() if len(cells) > 8 else ""

            # Cell 9 = Type (New Connection, etc)
            ticket_type = (cells[9].text_content() or "").strip() if len(cells) > 9 else ""

            # Cell 12 = Department (HDC)
            department = (cells[12].text_content() or "").strip() if len(cells) > 12 else ""

            # Cell 13 = Reply Due (KPI)
            kpi = (cells[13].text_content() or "").strip() if len(cells) > 13 else ""

            # Parse date
            ticket_time = self._parse_date(date_text)

            # Address comes from the detail page, which we only open for NEW tickets.
            # Known tickets register presence from the grid row alone (no navigation);
            # notes are never fetched (updates come from Znuny).
            notes = None
            address = None

            if internal_id and not self.is_known_ticket(internal_id):
                self.logger.info(f"Getting address for new ticket {display_id} (ID: {internal_id})")
                _, address = self._get_ticket_details(internal_id)

            return Ticket(
                portal="rol",
                ticket_id=internal_id,  # ROL internal ticket ID (e.g., 115385)
                address=address,
                account=display_id,  # ROL display ID (e.g., ROL250141)
                customer_name=customer_name,
                ticket_type=ticket_type,
                portal_created_at=ticket_time,
                service_type=department,
                status="Open",  # We're viewing Open tickets
                kpi=kpi,
                notes=notes
            )

        except Exception as e:
            self.logger.warning(f"Error parsing grid row: {e}")
            return None

    def _get_ticket_details(self, internal_id: str) -> tuple[str | None, str | None]:
        """Navigate to ticket detail page and extract notes and address."""
        notes = None
        address = None

        try:
            # Store current URL to navigate back
            current_url = self.browser.page.url
            detail_url = f"https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{internal_id}/inbox/55/4/-1"

            self.browser.page.goto(detail_url, timeout=self.NAV_TIMEOUT_MS)
            time.sleep(2)

            # Wait for page to load
            self.browser.page.wait_for_selector(self.TICKET_POST_CONTAINER_SELECTOR, timeout=self.NAV_TIMEOUT_MS)

            # Get post content (contains the ticket description/notes)
            try:
                post_container = self.browser.page.query_selector(self.TICKET_POST_CONTAINER_SELECTOR)
                post_text = (post_container.text_content() or "").strip() if post_container else ""

                if post_text:
                    notes = post_text
                    # Try to extract address from "Location -" line
                    for line in post_text.split('\n'):
                        line_lower = line.lower()
                        if 'location' in line_lower and '-' in line:
                            address = line.split('-', 1)[1].strip()
                            break
            except Exception as e:
                self.logger.debug(f"Error getting post content: {e}")

            # Navigate back to grid
            self.browser.page.goto(current_url, timeout=self.NAV_TIMEOUT_MS)
            time.sleep(1)

            # Wait for grid to reload
            self.browser.page.wait_for_selector(self.GRID_CONTAINER_SELECTOR, timeout=self.NAV_TIMEOUT_MS)

        except Exception as e:
            self.logger.debug(f"Error getting ticket details: {e}")

        return notes, address

    def _parse_date(self, date_str: str) -> datetime | None:
        """Parse ROL date format."""
        if not date_str:
            return None

        try:
            # Format: "27 January 2026 03:28 PM"
            return datetime.strptime(date_str, "%d %B %Y %I:%M %p")
        except ValueError:
            pass

        try:
            # Try alternate format with short month
            return datetime.strptime(date_str, "%d %b %Y %I:%M %p")
        except ValueError:
            pass

        self.logger.warning(f"Could not parse date: {date_str}")
        return None

    def logout(self):
        """Logout from ROL portal."""
        try:
            self.wait_and_click(self.LOGOUT_SELECTOR)
            time.sleep(1)
            self.logger.info("Logged out successfully")
        except Exception as e:
            self.logger.warning(f"Logout error: {e}")

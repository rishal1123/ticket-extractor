import time
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

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
            self.navigate_to(self.config.url)
            time.sleep(2)

            # Check if already logged in
            if self.is_logged_in():
                self.logger.info("Already logged in")
                return True

            # Enter username
            if not self.wait_and_type(By.CSS_SELECTOR, self.LOGIN_USERNAME_SELECTOR, self.config.username):
                self.logger.error("Failed to enter username")
                return False

            # Enter password
            if not self.wait_and_type(By.CSS_SELECTOR, self.LOGIN_PASSWORD_SELECTOR, self.config.password):
                self.logger.error("Failed to enter password")
                return False

            # Click login button
            if not self.wait_and_click(By.CSS_SELECTOR, self.LOGIN_BUTTON_SELECTOR):
                self.logger.error("Failed to click login button")
                return False

            # Wait for login to complete
            time.sleep(5)

            # Verify login success
            if "login" in self.browser.driver.current_url.lower():
                self.logger.error("Login failed - still on login page")
                return False

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return False

    def is_logged_in(self) -> bool:
        """Check if currently logged in by looking for logout button."""
        try:
            logout_elements = self.browser.driver.find_elements(By.CSS_SELECTOR, self.LOGOUT_SELECTOR)
            return len(logout_elements) > 0
        except:
            return False

    def extract_tickets(self) -> list[Ticket]:
        """Extract tickets from the ROL grid."""
        tickets = []

        try:
            # Navigate to Open tickets page
            self.logger.info("Navigating to Open tickets")
            self.navigate_to(self.OPEN_TICKETS_URL)
            time.sleep(3)

            # Wait for grid to load
            WebDriverWait(self.browser.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.GRID_CONTAINER_SELECTOR))
            )

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
            rows = self.browser.driver.find_elements(By.CSS_SELECTOR, self.GRID_ROW_SELECTOR)
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
            cells = row.find_elements(By.TAG_NAME, "td")

            if len(cells) < 14:
                self.logger.warning(f"Row {index} has insufficient cells: {len(cells)}")
                return None

            # Extract data from cells (0-indexed)
            # Cell 5 = Date
            date_text = cells[5].text.strip() if len(cells) > 5 else ""

            # Cell 6 = Display ID (ROL250141)
            display_id = ""
            try:
                link = cells[6].find_element(By.TAG_NAME, "a")
                display_id = link.text.strip()
            except:
                display_id = cells[6].text.strip() if len(cells) > 6 else ""

            if not display_id:
                self.logger.warning(f"Row {index} has no display ID")
                return None

            # Cell 7 = Owner/Customer name
            customer_name = cells[7].text.strip() if len(cells) > 7 else ""

            # Cell 8 = Priority
            priority = cells[8].text.strip() if len(cells) > 8 else ""

            # Cell 9 = Type (New Connection, etc)
            ticket_type = cells[9].text.strip() if len(cells) > 9 else ""

            # Cell 12 = Department (HDC)
            department = cells[12].text.strip() if len(cells) > 12 else ""

            # Cell 13 = Reply Due (KPI)
            kpi = cells[13].text.strip() if len(cells) > 13 else ""

            # Parse date
            ticket_time = self._parse_date(date_text)

            # Get ticket detail for notes and address
            notes = None
            address = None

            if internal_id:
                self.logger.info(f"Getting details for ticket {display_id} (ID: {internal_id})")
                notes, address = self._get_ticket_details(internal_id)

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
            current_url = self.browser.driver.current_url
            detail_url = f"https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{internal_id}/inbox/55/4/-1"

            self.browser.driver.get(detail_url)
            time.sleep(2)

            # Wait for page to load
            WebDriverWait(self.browser.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.TICKET_POST_CONTAINER_SELECTOR))
            )

            # Get post content (contains the ticket description/notes)
            try:
                post_container = self.browser.driver.find_element(By.CSS_SELECTOR, self.TICKET_POST_CONTAINER_SELECTOR)
                post_text = post_container.text.strip()

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
            self.browser.driver.get(current_url)
            time.sleep(1)

            # Wait for grid to reload
            WebDriverWait(self.browser.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.GRID_CONTAINER_SELECTOR))
            )

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
            self.wait_and_click(By.CSS_SELECTOR, self.LOGOUT_SELECTOR)
            time.sleep(1)
            self.logger.info("Logged out successfully")
        except Exception as e:
            self.logger.warning(f"Logout error: {e}")

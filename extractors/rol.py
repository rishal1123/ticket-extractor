import time
from datetime import datetime
from selenium.webdriver.common.by import By

from .base import BaseExtractor
from models.ticket import Ticket


class ROLExtractor(BaseExtractor):
    """Extractor for ROL portal."""

    # ============================================================
    # CSS SELECTORS - Update these based on actual portal HTML
    # ============================================================

    # Login page selectors
    LOGIN_USERNAME_SELECTOR = "#username"  # TODO: Update with actual selector
    LOGIN_PASSWORD_SELECTOR = "#password"  # TODO: Update with actual selector
    LOGIN_BUTTON_SELECTOR = "#loginButton"  # TODO: Update with actual selector

    # Navigation selectors
    TICKETS_MENU_SELECTOR = "a[href*='tickets']"  # TODO: Update with actual selector

    # Ticket list selectors
    TICKET_TABLE_SELECTOR = "table.tickets-table"  # TODO: Update with actual selector
    TICKET_ROW_SELECTOR = "table.tickets-table tbody tr"  # TODO: Update with actual selector

    # Ticket detail selectors (within each row)
    TICKET_ID_SELECTOR = "td.ticket-id"  # TODO: Update with actual selector
    ADDRESS_SELECTOR = "td.address"  # TODO: Update with actual selector
    CUSTOMER_NAME_SELECTOR = "td.customer-name"  # TODO: Update with actual selector
    TICKET_TYPE_SELECTOR = "td.ticket-type"  # TODO: Update with actual selector
    TIME_SELECTOR = "td.time"  # TODO: Update with actual selector
    SERVICE_TYPE_SELECTOR = "td.service-type"  # TODO: Update with actual selector
    STATUS_SELECTOR = "td.status"  # TODO: Update with actual selector
    KPI_SELECTOR = "td.kpi"  # TODO: Update with actual selector
    NOTES_SELECTOR = "td.notes"  # TODO: Update with actual selector

    # Pagination selectors
    NEXT_PAGE_SELECTOR = "a.next-page"  # TODO: Update with actual selector
    PAGINATION_SELECTOR = ".pagination"  # TODO: Update with actual selector

    # Logout selector
    LOGOUT_SELECTOR = "a[href*='logout']"  # TODO: Update with actual selector

    # ============================================================

    def login(self) -> bool:
        self.logger.info(f"Logging into ROL portal: {self.config.url}")

        try:
            self.navigate_to(self.config.url)
            time.sleep(2)

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
            time.sleep(3)

            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return False

    def extract_tickets(self) -> list[Ticket]:
        tickets = []

        try:
            # Navigate to tickets page if needed
            if self.TICKETS_MENU_SELECTOR:
                self.wait_and_click(By.CSS_SELECTOR, self.TICKETS_MENU_SELECTOR)
                time.sleep(2)

            # Extract tickets from all pages
            while True:
                page_tickets = self._extract_tickets_from_page()
                tickets.extend(page_tickets)

                if not self._go_to_next_page():
                    break

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return tickets

    def _extract_tickets_from_page(self) -> list[Ticket]:
        tickets = []

        try:
            rows = self.find_elements(By.CSS_SELECTOR, self.TICKET_ROW_SELECTOR)
            self.logger.info(f"Found {len(rows)} ticket rows on current page")

            for row in rows:
                try:
                    ticket = self._parse_ticket_row(row)
                    if ticket:
                        tickets.append(ticket)
                except Exception as e:
                    self.logger.warning(f"Error parsing ticket row: {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error extracting tickets from page: {e}")

        return tickets

    def _parse_ticket_row(self, row) -> Ticket | None:
        try:
            ticket_id = self._get_cell_text(row, self.TICKET_ID_SELECTOR)
            if not ticket_id:
                return None

            address = self._get_cell_text(row, self.ADDRESS_SELECTOR)
            customer_name = self._get_cell_text(row, self.CUSTOMER_NAME_SELECTOR)
            ticket_type = self._get_cell_text(row, self.TICKET_TYPE_SELECTOR)
            time_str = self._get_cell_text(row, self.TIME_SELECTOR)
            service_type = self._get_cell_text(row, self.SERVICE_TYPE_SELECTOR)
            status = self._get_cell_text(row, self.STATUS_SELECTOR)
            kpi = self._get_cell_text(row, self.KPI_SELECTOR)
            notes = self._get_cell_text(row, self.NOTES_SELECTOR)

            ticket_time = None
            if time_str:
                try:
                    ticket_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    self.logger.warning(f"Could not parse time: {time_str}")

            return Ticket(
                portal="rol",
                ticket_id=ticket_id,
                address=address,
                customer_name=customer_name,
                ticket_type=ticket_type,
                time=ticket_time,
                service_type=service_type,
                status=status,
                kpi=kpi,
                notes=notes
            )

        except Exception as e:
            self.logger.warning(f"Error parsing ticket: {e}")
            return None

    def _get_cell_text(self, row, selector: str) -> str | None:
        try:
            element = row.find_element(By.CSS_SELECTOR, selector)
            return element.text.strip() if element else None
        except:
            return None

    def _go_to_next_page(self) -> bool:
        try:
            next_btn = self.browser.driver.find_element(By.CSS_SELECTOR, self.NEXT_PAGE_SELECTOR)
            if next_btn and next_btn.is_enabled():
                next_btn.click()
                time.sleep(2)
                return True
        except:
            pass
        return False

    def logout(self):
        try:
            self.wait_and_click(By.CSS_SELECTOR, self.LOGOUT_SELECTOR)
            time.sleep(1)
        except Exception as e:
            self.logger.warning(f"Logout error: {e}")

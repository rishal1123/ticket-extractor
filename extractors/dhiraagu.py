import time

from .base import BaseExtractor
from models.ticket import Ticket
from config import Config


class DhiraaguExtractor(BaseExtractor):
    """Extractor for Dhiraagu portal (AFAS system - Filament PHP)."""

    # A realistic Chrome UA (NOT the Playwright "HeadlessChrome" default) so the
    # browser clears Cloudflare's JS challenge on its own — proven: this UA +
    # stealth evasions reaches the AFAS login page without FlareSolverr cookies.
    REALISTIC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    # Self-solve Cloudflare via stealth; FlareSolverr's IP-bound cookies hurt here.
    USE_FLARESOLVERR_FALLBACK = False

    def browser_user_agent(self):
        return self.REALISTIC_UA

    # ============================================================
    # CSS SELECTORS
    # ============================================================

    # Pre-login selector (click Third Party button to show login form)
    THIRDPARTY_BUTTON_XPATH = "xpath=//button[.//span[contains(text(), 'Third Party')]]"

    # Login page selectors
    LOGIN_USERNAME_SELECTOR = "#email"
    LOGIN_PASSWORD_SELECTOR = "#password"
    LOGIN_BUTTON_SELECTOR = "button[type='submit']"

    # Orders/Tickets page URL
    ORDERS_PAGE_URL = "https://afas.dhiraagu.com.mv/orders/hdc"

    # Filament table selectors
    TABLE_SELECTOR = "table.filament-tables-table"
    TABLE_ROW_SELECTOR = "table.filament-tables-table tbody tr"

    # Pagination - Filament uses wire:click for pagination
    NEXT_PAGE_XPATH = "xpath=//button[contains(@wire:click, 'nextPage')]"

    # Notes section selectors (on detail page)
    NOTES_CONTAINER_SELECTOR = ".filament-forms-rich-editor-component, .prose, [wire\\:model*='notes'], .filament-infolists-component-container"
    NOTES_LIST_SELECTOR = "table.filament-tables-table tbody tr"  # Notes might be in a table too

    # ============================================================

    # Column indices in the table (0-based)
    COL_SERVICE_NUM = 0    # Service #
    COL_ORDER_NUM = 1      # Order #
    COL_ADDRESS = 2        # Address
    COL_SERVICE_TYPE = 3   # Service type
    COL_ORDER_TYPE = 4     # Order type (New Service, Relocation, etc.)
    COL_STATUS = 5         # Status
    COL_CUSTOMER_NAME = 6  # Customer name
    COL_PRIORITY = 7       # Priority (Normal, etc.)
    COL_SUBMITTED_DATE = 8 # Submitted date (portal_created_at)
    COL_KPI = 9            # KPI (e.g., "30 Hr 40 Min")

    def detail_url(self, ticket) -> str:
        return f"{self.ORDERS_PAGE_URL}/{ticket.ticket_id}?activeRelationManager=notes"

    def is_logged_in(self) -> bool:
        """Check if currently logged in to Dhiraagu portal.

        Checks via the LOGIN page, NOT the protected /orders/hdc route: visiting
        /orders/hdc while unauthenticated triggers a harder Cloudflare challenge
        that poisons the browser so even /login then fails (verified). If already
        authenticated, Dhiraagu redirects away from /login; otherwise the login
        form is shown.
        """
        try:
            self.navigate_to(self.config.url)  # the /login URL
            time.sleep(2)
            current_url = self.browser.page.url.lower()
            # Redirected away from /login => we have a valid session.
            if "/login" not in current_url:
                self.logger.info("Session active - redirected away from login page")
                return True
            # Still on the login page (form/Third Party button) => not logged in.
            return False
        except Exception as e:
            self.logger.debug(f"Session check failed: {e}")
            return False

    def login(self) -> bool:
        self.logger.info(f"Logging into Dhiraagu portal: {self.config.url}")

        try:
            # is_logged_in() may have already loaded the /login page past
            # Cloudflare — reuse it to avoid a second (slow, flaky) CF round-trip.
            if not self.browser.page.query_selector(self.THIRDPARTY_BUTTON_XPATH):
                self.navigate_to(self.config.url)
            time.sleep(2)

            # Click "Third Party" button to show login form. Wait generously — the
            # Filament page can still be settling right after a Cloudflare clear.
            self.logger.info("Clicking Third Party button...")
            if not self.wait_and_click(self.THIRDPARTY_BUTTON_XPATH, timeout=20):
                self.logger.error("Failed to click Third Party button")
                return False

            # The login form loads via Livewire after the click — wait for the
            # email field to render before typing.
            if not self.browser.wait_for_element(self.LOGIN_USERNAME_SELECTOR, timeout=20):
                self.logger.error("Login form (email field) did not render after Third Party")
                return False

            # Enter username
            if not self.wait_and_type(self.LOGIN_USERNAME_SELECTOR, self.config.username):
                self.logger.error("Failed to enter username")
                return False

            # Enter password
            if not self.wait_and_type(self.LOGIN_PASSWORD_SELECTOR, self.config.password):
                self.logger.error("Failed to enter password")
                return False

            # Click login button
            if not self.wait_and_click(self.LOGIN_BUTTON_SELECTOR):
                self.logger.error("Failed to click login button")
                return False

            # Wait for login to complete
            time.sleep(3)

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return False

    # Clickable link selector in table rows (order number column)
    ORDER_LINK_SELECTOR = "a[href*='/orders/hdc/']"

    def extract_tickets(self) -> list[Ticket]:
        tickets = []

        try:
            # Navigate to orders page
            self.logger.info(f"Navigating to orders page: {self.ORDERS_PAGE_URL}")
            self.navigate_to(self.ORDERS_PAGE_URL)
            time.sleep(3)

            # Wait for table to load
            self.browser.wait_for_element(self.TABLE_SELECTOR, timeout=15)
            time.sleep(2)

            page_num = 1

            while True:
                self.logger.info(f"Processing orders on page {page_num}...")

                # Process each row on the current page by clicking on links
                page_tickets = self._process_orders_on_page()
                tickets.extend(page_tickets)
                self.logger.info(f"Extracted {len(page_tickets)} tickets from page {page_num}")

                # Check for next page
                if not self._go_to_next_page():
                    break
                page_num += 1
                time.sleep(2)

            self.logger.info(f"Total tickets extracted: {len(tickets)}")

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return tickets

    def _process_orders_on_page(self) -> list[Ticket]:
        """Process each order on the current page by clicking on links."""
        tickets = []

        try:
            time.sleep(1)

            # Get the count of rows first
            rows = self.find_elements(self.TABLE_ROW_SELECTOR)
            row_count = len(rows)
            self.logger.info(f"Found {row_count} rows on current page")

            # Process each row by index (re-find elements after each navigation)
            for row_idx in range(row_count):
                try:
                    # Re-find rows after returning from detail page
                    rows = self.find_elements(self.TABLE_ROW_SELECTOR)
                    if row_idx >= len(rows):
                        self.logger.warning(f"Row index {row_idx} out of range, skipping")
                        continue

                    row = rows[row_idx]
                    cells = row.query_selector_all("td")

                    if len(cells) < 10:
                        continue

                    # Extract submitted date from table and parse it
                    submitted_date_str = self._get_cell_text_by_element(cells[self.COL_SUBMITTED_DATE]) if len(cells) > self.COL_SUBMITTED_DATE else None
                    portal_created_at = self._parse_date(submitted_date_str) if submitted_date_str else None

                    # Extract status from table
                    status = self._get_cell_text_by_element(cells[self.COL_STATUS]) if len(cells) > self.COL_STATUS else None

                    # Collect basic data from the row first
                    order_data = {
                        'order_num': self._get_cell_text_by_element(cells[self.COL_ORDER_NUM]),
                        'service_num': self._get_cell_text_by_element(cells[self.COL_SERVICE_NUM]),
                        'address': self._get_cell_text_by_element(cells[self.COL_ADDRESS]),
                        'service_type': self._get_cell_text_by_element(cells[self.COL_SERVICE_TYPE]),
                        'order_type': self._get_cell_text_by_element(cells[self.COL_ORDER_TYPE]),
                        'status': status,
                        'portal_created_at': portal_created_at,
                        'kpi': self._get_cell_text_by_element(cells[self.COL_KPI]) if len(cells) > self.COL_KPI else None,
                        'customer_name': self._get_cell_text_by_element(cells[self.COL_CUSTOMER_NAME]) if len(cells) > self.COL_CUSTOMER_NAME else None,
                    }

                    if not order_data['service_num']:
                        continue

                    ticket_id = order_data['service_num']

                    # Known ticket: register presence only, skip the detail-page
                    # navigation. Notes are never fetched — updates come from Znuny.
                    if self.is_known_ticket(ticket_id):
                        tickets.append(self.presence_ticket(ticket_id))
                        continue

                    self.logger.info(f"Processing new order {row_idx + 1}/{row_count}: {ticket_id}")

                    # Find and click the link in this row
                    link = row.query_selector_all(self.ORDER_LINK_SELECTOR)
                    if not link:
                        # Try clicking on the order number cell directly
                        link = cells[self.COL_ORDER_NUM].query_selector_all("a")

                    if link:
                        link[0].click()
                        time.sleep(2)

                        # Extract details from the detail page
                        ticket = self._extract_from_detail_page(order_data)
                        if ticket:
                            tickets.append(ticket)

                        # Navigate back to the orders list
                        self.navigate_to(self.ORDERS_PAGE_URL)
                        time.sleep(2)
                        self.browser.wait_for_element(self.TABLE_SELECTOR, timeout=15)
                        time.sleep(1)
                    else:
                        self.logger.warning(f"No clickable link found for order {order_data['order_num']}")

                except Exception as e:
                    self.logger.warning(f"Error processing row {row_idx}: {e}")
                    # Try to navigate back to orders page
                    try:
                        self.navigate_to(self.ORDERS_PAGE_URL)
                        time.sleep(2)
                    except Exception:
                        pass
                    continue

        except Exception as e:
            self.logger.error(f"Error processing orders on page: {e}")

        return tickets

    def _extract_from_detail_page(self, order_data: dict) -> Ticket | None:
        """Extract ticket data from the detail page."""
        try:
            # Capture the current URL (detail page URL)
            portal_url = self.browser.page.url

            # Extract additional details from the form
            detail_data = self._extract_detail_page_data()

            # Notes are intentionally not fetched (updates come from Znuny)
            notes = None

            # Merge data - prefer detail page data if available
            customer_name = detail_data.get('customer_name') or order_data.get('customer_name')
            address = self._build_address(detail_data) or order_data.get('address')

            # Use portal_created_at from table row, fallback to detail page
            portal_created_at = order_data.get('portal_created_at') or detail_data.get('portal_created_at')

            # Create ticket with all data
            return Ticket(
                portal="dhiraagu",
                ticket_id=order_data['service_num'],  # Service Number as ticket ID
                address=address,
                account=order_data.get('order_num'),  # Order Number as account
                customer_name=customer_name,
                ticket_type=order_data.get('order_type'),
                portal_created_at=portal_created_at,
                service_type=order_data.get('service_type'),
                status=order_data.get('status'),
                kpi=order_data.get('kpi'),
                notes=notes,
                portal_url=portal_url
            )

        except Exception as e:
            self.logger.warning(f"Error extracting from detail page: {e}")
            return None

    def _extract_detail_page_data(self) -> dict:
        """Extract data from form fields on the detail page."""
        data = {}
        try:
            # Wait for form to render before querying fields
            self.browser.page.wait_for_selector("[id^='data.']", timeout=5000)
        except Exception:
            self.logger.debug("Detail page form fields not found within timeout")
        try:
            # Customer name
            customer_name_input = self.browser.page.query_selector("[id='data.customer_name']")
            if customer_name_input:
                data['customer_name'] = customer_name_input.get_attribute('value')

            # Contact number
            contact_input = self.browser.page.query_selector("[id='data.contact_number']")
            if contact_input:
                data['contact_number'] = contact_input.get_attribute('value')

            # Building
            building_input = self.browser.page.query_selector("[id='data.building']")
            if building_input:
                data['building'] = building_input.get_attribute('value')

            # Floor
            floor_input = self.browser.page.query_selector("[id='data.floor']")
            if floor_input:
                data['floor'] = floor_input.get_attribute('value')

            # Apartment
            apartment_input = self.browser.page.query_selector("[id='data.apartment']")
            if apartment_input:
                data['apartment'] = apartment_input.get_attribute('value')

            # Try to extract portal creation date (created_at field)
            created_at_input = self.browser.page.query_selector("[id='data.created_at']")
            if not created_at_input:
                created_at_input = self.browser.page.query_selector("[id*='created_at'], [name*='created_at']")
            if created_at_input:
                created_str = created_at_input.get_attribute('value') or (created_at_input.text_content() or "")
                if created_str:
                    data['portal_created_at'] = self._parse_date(created_str)

        except Exception as e:
            self.logger.warning(f"Error extracting detail page data: {e}")

        return data

    def _parse_date(self, date_str: str):
        """Parse date string to datetime."""
        from datetime import datetime
        formats = [
            "%d-%b-%Y",          # 02-Feb-2026
            "%d-%b-%Y %H:%M:%S", # 02-Feb-2026 10:30:00
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue
        self.logger.debug(f"Could not parse date: {date_str}")
        return None

    def _build_address(self, detail_data: dict) -> str | None:
        """Build address string from detail data."""
        parts = []
        if detail_data.get('building'):
            parts.append(detail_data['building'])
        if detail_data.get('floor'):
            parts.append(f"Floor {detail_data['floor']}")
        if detail_data.get('apartment'):
            parts.append(f"Apt {detail_data['apartment']}")

        return ", ".join(parts) if parts else None

    def _extract_notes_from_detail_page(self) -> str | None:
        """Extract notes from the order detail page."""
        try:
            time.sleep(1)

            notes_list = []

            # Find notes in the Filament relation manager table
            # Each note is in a row with structure:
            # - div.font-bold = source (e.g., "HDC")
            # - div.py-2.whitespace-pre-wrap = note text
            # - div.text-sm.italic = author and date
            note_rows = self.browser.page.query_selector_all(
                ".filament-tables-row .filament-tables-cell .whitespace-normal"
            )

            for note_container in note_rows:
                try:
                    # Extract source
                    source = ""
                    source_elem = note_container.query_selector_all(".font-bold")
                    if source_elem:
                        source = (source_elem[0].text_content() or "").strip()

                    # Extract note text
                    note_text = ""
                    text_elem = note_container.query_selector_all(".whitespace-pre-wrap")
                    if text_elem:
                        note_text = (text_elem[0].text_content() or "").strip()

                    # Extract author and date
                    author_date = ""
                    author_elem = note_container.query_selector_all(".italic")
                    if author_elem:
                        author_date = (author_elem[0].text_content() or "").strip()

                    if note_text:
                        note_entry = f"[{source}] {note_text}"
                        if author_date:
                            note_entry += f"\n{author_date}"
                        notes_list.append(note_entry)

                except Exception as e:
                    self.logger.debug(f"Error parsing note: {e}")
                    continue

            # Fallback: try to get all text from note rows
            if not notes_list:
                rows = self.browser.page.query_selector_all(
                    ".filament-tables-row"
                )
                for row in rows:
                    try:
                        text = (row.text_content() or "").strip()
                        if text and len(text) > 5:
                            notes_list.append(text)
                    except Exception:
                        continue

            if notes_list:
                return "\n---\n".join(notes_list)

            return None

        except Exception as e:
            self.logger.warning(f"Error extracting notes: {e}")
            return None

    def _get_cell_text_by_element(self, cell) -> str | None:
        """Get text from a cell element."""
        try:
            text = (cell.text_content() or "").strip()
            return text if text else None
        except Exception:
            return None

    def _go_to_next_page(self) -> bool:
        """Navigate to next page of tickets. Returns False if no more pages."""
        try:
            # Filament pagination - look for next page button
            next_buttons = self.browser.page.query_selector_all(self.NEXT_PAGE_XPATH)

            for btn in next_buttons:
                if btn.is_visible() and btn.is_enabled():
                    classes = btn.get_attribute("class") or ""
                    if "disabled" not in classes and "cursor-not-allowed" not in classes:
                        btn.click()
                        time.sleep(2)
                        return True

            # Alternative: Look for "Next" link in pagination
            next_links = self.browser.page.query_selector_all(
                "xpath=//nav[contains(@class, 'pagination')]//a[contains(text(), 'Next') or @rel='next']"
            )
            for link in next_links:
                if link.is_visible():
                    link.click()
                    time.sleep(2)
                    return True

            return False

        except Exception as e:
            self.logger.debug(f"No more pages or pagination error: {e}")
            return False

    def logout(self):
        try:
            self.logger.info("Logging out...")
            user_menu = self.browser.page.query_selector_all("[data-dropdown-trigger]")
            if user_menu:
                user_menu[0].click()
                time.sleep(1)

            logout_link = self.browser.page.query_selector_all(
                "xpath=//a[contains(@href, 'logout')] | //button[contains(text(), 'Sign out') or contains(text(), 'Logout')]"
            )
            if logout_link:
                logout_link[0].click()
                time.sleep(1)

        except Exception as e:
            self.logger.warning(f"Logout error: {e}")

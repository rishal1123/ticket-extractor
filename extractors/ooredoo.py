import time
from datetime import datetime
from selenium.webdriver.common.by import By

from .base import BaseExtractor
from models.ticket import Ticket


class OoredooExtractor(BaseExtractor):
    """Extractor for Ooredoo portal (CBSMW - CBS Middleware)."""

    # ============================================================
    # PORTAL URLS
    # ============================================================
    LOGIN_URL = "https://www.ooredoo.mv/webapps/FMS/public/login"
    TICKETS_URL = "https://www.ooredoo.mv/webapps/FMS/public/tickets"
    TICKET_DETAIL_URL = "https://www.ooredoo.mv/webapps/FMS/public/tickets/ticket_info/{ticket_id}"

    # ============================================================
    # CSS SELECTORS
    # ============================================================

    # Login page selectors
    LOGIN_EMAIL_SELECTOR = "#email"
    LOGIN_PASSWORD_SELECTOR = "#password"
    LOGIN_BUTTON_SELECTOR = "button[type='submit']"

    # Ticket list page selectors
    TICKET_TABLE_SELECTOR = "#datatable"
    TICKET_ROW_SELECTOR = "#datatable tbody tr"
    TICKET_LINK_SELECTOR = "td:nth-child(2) a"  # Ticket ID is in 2nd column

    # Pagination selectors (DataTables)
    NEXT_PAGE_SELECTOR = "#datatable_next:not(.disabled)"
    PAGINATION_INFO_SELECTOR = "#datatable_info"

    # Logout selector
    LOGOUT_FORM_SELECTOR = "#logout-form"

    # Detail page selectors
    TICKET_FEED_TAB_SELECTOR = "a[href='#ticket_feedlink']"
    TICKET_FEED_CONTAINER_SELECTOR = "#ticket_feedlink"
    FEED_ENTRY_SELECTOR = "#ticket_feedlink .timeline-item, #ticket_feedlink .feed-item, #ticket_feedlink .card-body"

    # Comments tab selectors
    COMMENTS_TAB_SELECTOR = "a[href='#internalcommentlink1']"
    COMMENTS_TABLE_SELECTOR = "#internalcommentlink1 table tbody tr"

    # ============================================================
    # TABLE COLUMN INDICES (0-based)
    # ============================================================
    COL_AGING = 0           # "0 Days"
    COL_TICKET_ID = 1       # "152342"
    COL_ACCOUNT_ID = 2      # "40097792"
    COL_CREATED_DATE = 3    # "2026-02-03 20:52:34"
    COL_NAME = 4            # "Moosa Shareef"
    COL_ADDRESS = 5         # "H03-02-02"
    COL_MOBILE = 6          # "9609799228"
    COL_STATUS = 7          # "Open"
    COL_QUEUE = 8           # "Hulhumale Phase 2"
    COL_QUEUE_AGING = 9     # "0 Days"
    COL_CATEGORY = 10       # "Installation"
    COL_SUB_CATEGORY = 11   # "Survey Pending"
    COL_LAST_UPDATED = 12   # "2026-02-03 21:57:09"
    COL_ASSIGNED_TO = 13    # "-"
    COL_REGION = 14         # "Male"

    # ============================================================

    def is_logged_in(self) -> bool:
        """Check if currently logged in to Ooredoo portal."""
        try:
            # Navigate to tickets page and check if we can access it
            self.navigate_to(self.TICKETS_URL)
            time.sleep(2)

            # If redirected to login page, we're not logged in
            current_url = self.browser.driver.current_url
            if "login" in current_url:
                return False

            # Check if ticket table is present
            tables = self.find_elements(By.CSS_SELECTOR, self.TICKET_TABLE_SELECTOR)
            return len(tables) > 0

        except Exception as e:
            self.logger.warning(f"Error checking login status: {e}")
            return False

    def login(self) -> bool:
        self.logger.info(f"Logging into Ooredoo portal: {self.config.url}")

        try:
            self.navigate_to(self.LOGIN_URL)
            time.sleep(2)

            # Enter email
            if not self.wait_and_type(By.CSS_SELECTOR, self.LOGIN_EMAIL_SELECTOR, self.config.username):
                self.logger.error("Failed to enter email")
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

            # Verify login succeeded
            current_url = self.browser.driver.current_url
            if "login" in current_url:
                self.logger.error("Login failed - still on login page")
                self.take_screenshot("login_failed")
                return False

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            self.take_screenshot("login_error")
            return False

    def extract_tickets(self) -> list[Ticket]:
        tickets = []

        try:
            # Navigate to tickets page
            self.navigate_to(self.TICKETS_URL)
            time.sleep(3)

            # Wait for DataTable to load
            self._wait_for_datatable()

            # Extract tickets from all pages
            page_num = 1
            while True:
                self.logger.info(f"Extracting tickets from page {page_num}")
                page_tickets = self._extract_tickets_from_page()
                tickets.extend(page_tickets)

                if not self._go_to_next_page():
                    break
                page_num += 1

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")
            self.take_screenshot("extraction_error")

        return tickets

    def _wait_for_datatable(self):
        """Wait for DataTable to be fully loaded."""
        try:
            for _ in range(10):
                rows = self.find_elements(By.CSS_SELECTOR, self.TICKET_ROW_SELECTOR)
                if rows:
                    # Check if first row is not a "Loading..." or "No data" message
                    first_row_text = rows[0].text.lower()
                    if "loading" not in first_row_text and "processing" not in first_row_text:
                        return
                time.sleep(1)
        except Exception as e:
            self.logger.warning(f"Error waiting for DataTable: {e}")

    def _extract_tickets_from_page(self) -> list[Ticket]:
        tickets = []

        try:
            rows = self.find_elements(By.CSS_SELECTOR, self.TICKET_ROW_SELECTOR)
            row_count = len(rows)
            self.logger.info(f"Found {row_count} ticket rows on current page")

            # Process each row by index (re-find after navigation)
            for row_idx in range(row_count):
                try:
                    # Re-find rows after returning from detail page
                    rows = self.find_elements(By.CSS_SELECTOR, self.TICKET_ROW_SELECTOR)
                    if row_idx >= len(rows):
                        self.logger.warning(f"Row index {row_idx} out of range, skipping")
                        continue

                    row = rows[row_idx]

                    # First extract basic data from the row
                    ticket_data = self._parse_ticket_row_data(row)
                    if not ticket_data or not ticket_data.get('ticket_id'):
                        continue

                    self.logger.info(f"Processing ticket {row_idx + 1}/{row_count}: {ticket_data['ticket_id']}")

                    # Click on the ticket to open detail page
                    cells = row.find_elements(By.TAG_NAME, "td")
                    ticket_link = cells[self.COL_TICKET_ID].find_elements(By.TAG_NAME, "a")

                    if ticket_link:
                        ticket_link[0].click()
                        time.sleep(2)

                        # Extract notes from detail page
                        notes = self._extract_notes_from_detail_page()

                        # Build the ticket with notes
                        ticket = self._build_ticket(ticket_data, notes)
                        if ticket:
                            tickets.append(ticket)

                        # Navigate back to tickets list
                        self.navigate_to(self.TICKETS_URL)
                        time.sleep(2)
                        self._wait_for_datatable()
                    else:
                        # No link, just create ticket without detail notes
                        ticket = self._build_ticket(ticket_data, None)
                        if ticket:
                            tickets.append(ticket)

                except Exception as e:
                    self.logger.warning(f"Error processing row {row_idx}: {e}")
                    # Try to navigate back to tickets page
                    try:
                        self.navigate_to(self.TICKETS_URL)
                        time.sleep(2)
                        self._wait_for_datatable()
                    except Exception:
                        pass
                    continue

        except Exception as e:
            self.logger.error(f"Error extracting tickets from page: {e}")

        return tickets

    def _parse_ticket_row_data(self, row) -> dict | None:
        """Extract data from a ticket row into a dictionary."""
        try:
            cells = row.find_elements(By.TAG_NAME, "td")

            if len(cells) < 12:
                return None

            ticket_id = self._get_cell_text(cells, self.COL_TICKET_ID)
            if not ticket_id:
                return None

            return {
                'ticket_id': ticket_id,
                'account_id': self._get_cell_text(cells, self.COL_ACCOUNT_ID),
                'customer_name': self._get_cell_text(cells, self.COL_NAME),
                'address': self._get_cell_text(cells, self.COL_ADDRESS),
                'status': self._get_cell_text(cells, self.COL_STATUS),
                'created_date_str': self._get_cell_text(cells, self.COL_CREATED_DATE),
                'category': self._get_cell_text(cells, self.COL_CATEGORY),
                'sub_category': self._get_cell_text(cells, self.COL_SUB_CATEGORY),
                'aging': self._get_cell_text(cells, self.COL_AGING),
                'queue': self._get_cell_text(cells, self.COL_QUEUE),
                'mobile': self._get_cell_text(cells, self.COL_MOBILE),
            }

        except Exception as e:
            self.logger.warning(f"Error parsing ticket row data: {e}")
            return None

    def _extract_notes_from_detail_page(self) -> str | None:
        """Extract notes from Comments tab and Ticket Feed tab on the detail page."""
        try:
            time.sleep(2)
            all_notes = []

            # First, extract from Comments tab (has full comment text)
            comments = self._extract_comments_from_tab()
            if comments:
                all_notes.extend(comments)

            # Then, extract from Ticket Feed tab (has activity log)
            feed_notes = self._extract_feed_entries()
            if feed_notes:
                all_notes.extend(feed_notes)

            if all_notes:
                return "\n---\n".join(all_notes)

            return None

        except Exception as e:
            self.logger.warning(f"Error extracting notes from detail page: {e}")
            return None

    def _extract_comments_from_tab(self) -> list[str]:
        """Extract comments from the Comments tab."""
        comments = []
        try:
            # Click on Comments tab - try multiple selectors
            comments_tabs = self.find_elements(By.CSS_SELECTOR, self.COMMENTS_TAB_SELECTOR)
            if not comments_tabs:
                comments_tabs = self.find_elements(By.XPATH, "//a[contains(text(), 'Comments')]")
            if not comments_tabs:
                comments_tabs = self.find_elements(By.XPATH, "//a[@href='#internalcommentlink1']")

            if comments_tabs:
                self.browser.driver.execute_script("arguments[0].click();", comments_tabs[0])
                time.sleep(2)

            # Extract comments from table - try broader selector
            comment_rows = self.find_elements(By.CSS_SELECTOR, "#internalcommentlink1 table tbody tr")
            if not comment_rows:
                comment_rows = self.find_elements(By.CSS_SELECTOR, "#internalcommentlink1 tr")

            self.logger.debug(f"Found {len(comment_rows)} comment rows")

            for row in comment_rows:
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) >= 3:
                        user = cells[0].text.strip() if cells[0].text else cells[0].get_attribute('textContent').strip()
                        comment = cells[1].text.strip() if cells[1].text else cells[1].get_attribute('textContent').strip()
                        date = cells[2].text.strip() if cells[2].text else cells[2].get_attribute('textContent').strip()

                        if comment and len(comment) > 5:
                            comments.append(f"[{user}] {comment} ({date})")
                            self.logger.debug(f"Extracted comment: {comment[:50]}...")
                except Exception as e:
                    self.logger.debug(f"Error parsing comment row: {e}")
                    continue

        except Exception as e:
            self.logger.debug(f"Error extracting comments: {e}")

        return comments

    def _extract_feed_entries(self) -> list[str]:
        """Extract entries from the Ticket Feed tab."""
        notes_list = []
        try:
            # Click on "Ticket Feed" tab
            feed_tabs = self.find_elements(By.CSS_SELECTOR, self.TICKET_FEED_TAB_SELECTOR)
            if not feed_tabs:
                feed_tabs = self.find_elements(By.XPATH, "//a[contains(text(), 'Ticket Feed')]")

            if feed_tabs:
                self.browser.driver.execute_script("arguments[0].click();", feed_tabs[0])
                time.sleep(1)

            # Extract feed entries from <li> elements
            feed_entries = self.find_elements(By.CSS_SELECTOR, "#ticket_feedlink li")

            if feed_entries:
                for entry in feed_entries:
                    try:
                        html = entry.get_attribute('innerHTML')
                        if html:
                            import re
                            text = re.sub(r'<[^>]+>', ' ', html)
                            text = re.sub(r'\s+', ' ', text).strip()
                            if text and len(text) > 5:
                                # Skip if it's a duplicate of a comment we already have
                                if "Commented:" not in text:
                                    notes_list.append(text)
                    except Exception:
                        continue

        except Exception as e:
            self.logger.debug(f"Error extracting feed entries: {e}")

        return notes_list

    def _build_ticket(self, data: dict, detail_notes: str | None) -> Ticket | None:
        """Build a Ticket object from extracted data."""
        try:
            # Parse created date
            portal_created_at = self._parse_date(data.get('created_date_str')) if data.get('created_date_str') else None

            # Build notes - combine queue/mobile info with detail notes
            notes_parts = []
            if data.get('queue'):
                notes_parts.append(f"Queue: {data['queue']}")
            if data.get('mobile'):
                notes_parts.append(f"Mobile: {data['mobile']}")

            basic_notes = " | ".join(notes_parts) if notes_parts else None

            # Combine with detail notes
            if detail_notes and basic_notes:
                notes = f"{basic_notes}\n---\n{detail_notes}"
            elif detail_notes:
                notes = detail_notes
            else:
                notes = basic_notes

            return Ticket(
                portal="ooredoo",
                ticket_id=data['ticket_id'],
                address=data.get('address'),
                account=data.get('account_id'),
                customer_name=data.get('customer_name'),
                ticket_type=data.get('category'),  # "Installation", etc.
                portal_created_at=portal_created_at,
                service_type=data.get('sub_category'),  # "Survey Pending", etc.
                status=data.get('status'),
                kpi=data.get('aging'),  # "0 Days", etc.
                notes=notes
            )

        except Exception as e:
            self.logger.warning(f"Error building ticket: {e}")
            return None

    def _get_cell_text(self, cells, index: int) -> str | None:
        """Get text from a cell by index."""
        try:
            if index < len(cells):
                text = cells[index].text.strip()
                return text if text and text != "-" else None
        except Exception:
            pass
        return None

    def _parse_date(self, date_str: str):
        """Parse date string to datetime."""
        if not date_str:
            return None

        formats = [
            "%Y-%m-%d %H:%M:%S",  # 2026-02-03 20:52:34
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str.strip(), fmt)
            except ValueError:
                continue

        self.logger.warning(f"Could not parse date: {date_str}")
        return None

    def _go_to_next_page(self) -> bool:
        """Navigate to next page if available."""
        try:
            # Check if next button exists and is enabled
            next_btns = self.find_elements(By.CSS_SELECTOR, self.NEXT_PAGE_SELECTOR)
            if next_btns and len(next_btns) > 0:
                next_btn = next_btns[0]
                if "disabled" not in next_btn.get_attribute("class"):
                    next_btn.click()
                    time.sleep(2)
                    self._wait_for_datatable()
                    return True
        except Exception as e:
            self.logger.debug(f"No more pages or error: {e}")
        return False

    def logout(self):
        """Logout from Ooredoo portal."""
        try:
            # Find and submit logout form
            logout_forms = self.find_elements(By.CSS_SELECTOR, self.LOGOUT_FORM_SELECTOR)
            if logout_forms:
                self.browser.driver.execute_script("arguments[0].submit();", logout_forms[0])
                time.sleep(1)
                self.logger.info("Logged out successfully")
            else:
                # Try clicking logout link
                logout_links = self.find_elements(By.XPATH, "//a[contains(@href, 'logout')]")
                if logout_links:
                    logout_links[0].click()
                    time.sleep(1)

        except Exception as e:
            self.logger.warning(f"Logout error: {e}")

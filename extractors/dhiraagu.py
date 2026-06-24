import time

from .base import BaseExtractor
from models.ticket import Ticket
from config import Config


class DhiraaguExtractor(BaseExtractor):
    """Extractor for Dhiraagu portal (AFAS system - Filament PHP)."""

    # Cloudflare flags HEADLESS browsers. We use FIREFOX run HEADED (under Xvfb in
    # the app container): Chrome's renderer crashes in the container (sad face), but
    # Gecko renders reliably headless on Linux AND passes Cloudflare when headed.
    # Firefox uses prefs, not a channel, so BROWSER_CHANNEL stays None.
    BROWSER_ENGINE = "firefox"
    FORCE_HEADED = True
    MEMORY_LIMIT_MB = 2000
    # Self-solve Cloudflare via the real headed browser; FlareSolverr's IP-bound
    # cookies hurt here, so don't fall back to it.
    USE_FLARESOLVERR_FALLBACK = False

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

            # Best-effort: show 100 rows/page (Filament default is 10) to reduce pages.
            self._set_records_per_page(100)

            # --- LISTING PHASE (URL-based pagination) ---
            # Walk pages via ?page=N (Livewire/Laravel honor the `page` query param)
            # collecting every row's data WITHOUT visiting detail pages, so the table
            # state is never disturbed. Dedupe by service_num and stop when a page
            # yields no NEW rows — robust whether or not the per-page select took
            # effect and regardless of the pagination-button markup.
            new_orders = []
            seen_ids = set()
            page_num = 1
            MAX_PAGES = 50
            while page_num <= MAX_PAGES:
                if page_num > 1:
                    self.navigate_to(f"{self.ORDERS_PAGE_URL}?page={page_num}")
                    time.sleep(2)
                    self.browser.wait_for_element(self.TABLE_SELECTOR, timeout=15)
                    time.sleep(1)
                page_orders = self._collect_page_orders()
                fresh = [o for o in page_orders if o['service_num'] not in seen_ids]
                self.logger.info(f"Page {page_num}: {len(page_orders)} rows ({len(fresh)} new)")
                if not fresh:
                    break
                for order_data in fresh:
                    seen_ids.add(order_data['service_num'])
                    if self.is_known_ticket(order_data['service_num']):
                        tickets.append(self.presence_ticket(order_data['service_num']))
                    else:
                        new_orders.append(order_data)
                page_num += 1

            self.logger.info(
                f"Listing done: {len(seen_ids)} tickets across {page_num} page(s) — "
                f"{len(tickets)} known present, {len(new_orders)} new to enrich"
            )

            # --- DETAIL PHASE ---
            # Enrich each NEW ticket from its own detail page via direct URL — no row
            # clicking, so the listing/pagination above is never disturbed.
            for order_data in new_orders:
                try:
                    href = order_data.get('detail_href')
                    if href:
                        self.navigate_to(href)
                        time.sleep(2)
                        ticket = self._extract_from_detail_page(order_data)
                    else:
                        ticket = self._build_ticket_from_order(order_data)
                    if ticket:
                        tickets.append(ticket)
                except Exception as e:
                    self.logger.warning(f"Error enriching {order_data.get('service_num')}: {e}")
                    fallback = self._build_ticket_from_order(order_data)
                    if fallback:
                        tickets.append(fallback)

            self.logger.info(f"Total tickets extracted: {len(tickets)}")

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return tickets

    def _collect_page_orders(self) -> list[dict]:
        """Read every row's data on the current page (no navigation). Returns a list
        of order_data dicts, each including 'detail_href' (absolute URL) for later
        detail enrichment. Reading-only keeps the table's per-page/page state stable."""
        orders = []
        try:
            time.sleep(1)
            rows = self.find_elements(self.TABLE_ROW_SELECTOR)
            for row in rows:
                try:
                    cells = row.query_selector_all("td")
                    if len(cells) < 10:
                        continue

                    submitted_date_str = self._get_cell_text_by_element(cells[self.COL_SUBMITTED_DATE]) if len(cells) > self.COL_SUBMITTED_DATE else None
                    order_data = {
                        'order_num': self._get_cell_text_by_element(cells[self.COL_ORDER_NUM]),
                        'service_num': self._get_cell_text_by_element(cells[self.COL_SERVICE_NUM]),
                        'address': self._get_cell_text_by_element(cells[self.COL_ADDRESS]),
                        'service_type': self._get_cell_text_by_element(cells[self.COL_SERVICE_TYPE]),
                        'order_type': self._get_cell_text_by_element(cells[self.COL_ORDER_TYPE]),
                        'status': self._get_cell_text_by_element(cells[self.COL_STATUS]) if len(cells) > self.COL_STATUS else None,
                        'portal_created_at': self._parse_date(submitted_date_str) if submitted_date_str else None,
                        'kpi': self._get_cell_text_by_element(cells[self.COL_KPI]) if len(cells) > self.COL_KPI else None,
                        'customer_name': self._get_cell_text_by_element(cells[self.COL_CUSTOMER_NAME]) if len(cells) > self.COL_CUSTOMER_NAME else None,
                    }
                    if not order_data['service_num']:
                        continue

                    # Capture the detail-page link (resolved to an absolute URL) so we
                    # can visit it directly later instead of clicking the row.
                    link = row.query_selector(self.ORDER_LINK_SELECTOR)
                    if not link and len(cells) > self.COL_ORDER_NUM:
                        link = cells[self.COL_ORDER_NUM].query_selector("a")
                    order_data['detail_href'] = None
                    if link:
                        try:
                            order_data['detail_href'] = link.evaluate("el => el.href")
                        except Exception:
                            pass

                    orders.append(order_data)
                except Exception as e:
                    self.logger.debug(f"Error reading row: {e}")
                    continue
        except Exception as e:
            self.logger.error(f"Error collecting page orders: {e}")
        return orders

    def _build_ticket_from_order(self, order_data: dict) -> "Ticket | None":
        """Build a Ticket from table-row data only (fallback when the detail page
        can't be visited). Notes are not fetched — updates come from Znuny."""
        try:
            return Ticket(
                portal="dhiraagu",
                ticket_id=order_data['service_num'],
                address=order_data.get('address'),
                account=order_data.get('order_num'),
                customer_name=order_data.get('customer_name'),
                ticket_type=order_data.get('order_type'),
                portal_created_at=order_data.get('portal_created_at'),
                service_type=order_data.get('service_type'),
                status=order_data.get('status'),
                kpi=order_data.get('kpi'),
                notes=None,
                portal_url=order_data.get('detail_href'),
            )
        except Exception as e:
            self.logger.warning(f"Error building ticket from order: {e}")
            return None

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

    def _set_records_per_page(self, count: int = 100) -> bool:
        """Set Filament's 'records per page' selector to a larger value (default 10)
        so we fetch far more rows per page. Tries the Livewire per-page <select>;
        falls back to any <select> whose options are pagination sizes (…/50/100)."""
        page = self.browser.page
        # Filament v3 uses wire:model.live, v2 uses wire:model; the attr value is
        # tableRecordsPerPage. Colons/dots in the attr name must be CSS-escaped.
        candidates = [
            "select[wire\\:model\\.live='tableRecordsPerPage']",
            "select[wire\\:model='tableRecordsPerPage']",
            "select[wire\\:model\\.live*='RecordsPerPage']",
            "select[wire\\:model*='RecordsPerPage']",
        ]
        try:
            select_el = None
            for sel in candidates:
                el = page.query_selector(sel)
                if el:
                    select_el = el
                    break
            # Fallback: any <select> offering a "100" (or numeric pagination) option.
            if select_el is None:
                for el in page.query_selector_all("select"):
                    vals = [o.get_attribute("value") for o in el.query_selector_all("option")]
                    vals = [v for v in vals if v and v.isdigit()]
                    if "100" in vals or (vals and max(map(int, vals)) >= 25):
                        select_el = el
                        break
            if select_el is None:
                self.logger.warning("Records-per-page selector not found - using portal default (10)")
                return False

            options = [o.get_attribute("value") for o in select_el.query_selector_all("option")]
            options = [o for o in options if o and o.isdigit()]
            if not options:
                return False
            target = str(count) if str(count) in options else max(options, key=int)

            select_el.select_option(target)
            self.logger.info(f"Set records per page to {target}")
            time.sleep(2)  # Livewire re-renders the table
            self.browser.wait_for_element(self.TABLE_SELECTOR, timeout=15)
            time.sleep(1)
            return True
        except Exception as e:
            self.logger.warning(f"Could not set records-per-page (using default): {e}")
            return False

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

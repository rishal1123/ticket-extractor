import time
import re
from datetime import datetime

from .base import BaseExtractor
from models.ticket import Ticket
from formatter.model.formatters import clean_building_code


class MedianetExtractor(BaseExtractor):
    """Extractor for Medianet CRM.COM portal.

    Uses the Service Requests List view (a flat, paginated table) rather than
    the Kanban board. The list's default filter ("Closed Status is false")
    already excludes closed tickets, and list rows link directly to the
    ticket detail page via a UUID href — no per-ticket-type iteration or
    card-matching-by-text is needed like the old board approach required.
    """

    # SPA-heavy portal — skip images. Reset browser if it exceeds 1.5GB.
    DISABLE_IMAGES = True
    MEMORY_LIMIT_MB = 1500

    # ============================================================
    # CSS SELECTORS
    # ============================================================

    # Login page selectors (two-step login: email first, then password)
    LOGIN_EMAIL_SELECTOR = "input[type='email'], input[name='username']"
    LOGIN_PASSWORD_SELECTOR = "input[type='password']"
    LOGIN_SUBMIT_SELECTOR = "button[type='submit']"

    BASE_URL = "https://app.crm.com"
    SERVICE_REQUESTS_LIST_URL = "https://app.crm.com/crm/service-requests-list"

    # List view selectors
    LIST_PAGE_READY_SELECTOR = "button[data-test='showFilters']"
    FILTER_BADGE_SELECTOR = "span.filter-badge:not(.border-dashed)"
    FILTER_DELETE_BUTTON_SELECTOR = "button[data-test^='delete-filter']"
    SHOWS_LABEL_SELECTOR = "span[data-test^='shows[']"
    ROWS_PER_PAGE_TOGGLE_SELECTOR = "button.btn-size-per-page"
    ROWS_PER_PAGE_ITEM_SELECTOR = ".dropdown-menu .dropdown-item"
    TICKET_LIST_CARD_SELECTOR = "a[data-test='serviceRequestLink']"
    NEXT_PAGE_SELECTOR = "li.page-item:not(.disabled) > a[data-test='nextPage']"

    # Ticket detail page selectors
    TICKET_NUMBER_SELECTOR = "span[data-test='serviceRequestNumber']"
    TICKET_STATUS_BADGE_SELECTOR = ".breadcrumb .badge"
    CONTACT_NAME_SELECTOR = "span[data-test='contact']"
    CONTACT_PHONE_SELECTOR = "span[data-test='contact-phone-number']"
    CONTACT_EMAIL_SELECTOR = "span[data-test='contact-email_address']"
    # Address selectors - note the mr-1 suffix in the data-test value
    CONTACT_ADDRESS_SELECTOR = "span[data-test='contact-address mr-1']"
    CONTACT_ADDRESS_NAME_SELECTOR = "span[data-test='contact-address-name mr-1']"
    CONTACT_ADDRESS_BADGE_SELECTOR = "span.badge[data-test='contact-address']"
    DESCRIPTION_SELECTOR = "textarea[data-test='textarea-description']"
    LOCATION_SELECTOR = "span[data-test='address']"
    TEAM_SELECTOR = "span[data-test='assignedTeamName']"
    QUEUE_NAME_SELECTOR = "span[data-test='queueName']"
    PRIORITY_SELECTOR = "span[data-test='priority']"
    RESOLVED_STATUS_SELECTOR = "span[data-test='resolved']"
    CLOSE_DATE_SELECTOR = "small[data-test='closeDate']"

    # Notes section
    NOTES_CARD_SELECTOR = "[data-test='notesCard']"
    NOTES_CONTENT_SELECTOR = "[data-test='notesCard'] .card-body"

    # ============================================================

    # Longer timeout for this SPA (default 10s is too short)
    SPA_TIMEOUT = 60000
    # Login page URL (lighter than the full SPA board)
    LOGIN_URL = "https://app.crm.com/account/login"

    def _goto_spa(self, url: str, timeout: int = None):
        """Navigate to a Medianet SPA page using commit wait strategy.

        The SPA at app.crm.com never fires 'load' or 'domcontentloaded' in time,
        so we use 'commit' (just wait for initial HTTP response) and then wait
        for actual content via wait_for_selector().
        """
        t = timeout or self.SPA_TIMEOUT
        self.browser.page.goto(url, wait_until="commit", timeout=t)

    def is_logged_in(self) -> bool:
        """Check if currently logged in to Medianet portal."""
        try:
            # Check if browser is available
            if not self.browser or not self.browser.page:
                self.logger.info("is_logged_in: No browser available")
                return False

            # Check current URL first before navigating
            try:
                current_url = self.browser.page.url
                self.logger.info(f"is_logged_in: Current URL before check: {current_url}")
            except Exception as e:
                self.logger.info(f"is_logged_in: Browser not responsive: {e}")
                return False

            # Navigate to login page (lighter than full SPA board)
            # If we're logged in, it will redirect to the app; if not, we stay on login
            self.logger.info(f"is_logged_in: Navigating to {self.LOGIN_URL}")
            try:
                self._goto_spa(self.LOGIN_URL)
            except Exception as nav_err:
                self.logger.warning(f"is_logged_in: Navigation failed: {nav_err}")
                return False

            # Wait for page JS to finish (SPA routing/redirect)
            try:
                self.browser.page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            time.sleep(2)

            # Check current URL after navigation
            current_url = self.browser.page.url
            self.logger.info(f"is_logged_in: URL after navigation: {current_url}")

            # If still on login page, we're not logged in
            if "/account/login" in current_url:
                self.logger.info("is_logged_in: On login page - session expired")
                return False

            # If redirected away from login, session is active
            # Now navigate to the service requests list and wait for it to load
            self.logger.info("is_logged_in: Session active, navigating to service requests list")
            self._goto_spa(self.SERVICE_REQUESTS_LIST_URL)
            time.sleep(3)

            try:
                self.browser.page.wait_for_selector(self.LIST_PAGE_READY_SELECTOR, timeout=30000)
                self.logger.info("Session valid - service requests list loaded successfully")
                return True
            except Exception:
                self.logger.info("is_logged_in: Service requests list did not load in time")

            return False
        except Exception as e:
            self.logger.warning(f"is_logged_in check failed with error: {e}")
            return False

    def login(self) -> bool:
        """Login to Medianet portal using two-step authentication."""
        self.logger.info(f"Logging into Medianet portal: {self.LOGIN_URL}")

        try:
            current_url = self.browser.page.url

            # Only navigate if not already on the login page
            # (is_logged_in() already navigated there, re-navigating causes
            # a page reload where wait_until="commit" returns before React renders)
            if "/account/login" not in current_url:
                self._goto_spa(self.LOGIN_URL)
                time.sleep(3)
                current_url = self.browser.page.url

            self.logger.info(f"login: Current URL: {current_url}")

            # Check if redirected away from login (already logged in)
            if "/account/login" not in current_url:
                self.logger.info("login: Session already active (not on login page) - reusing session")
                return True

            # Wait for page JS to finish executing (React form needs to render)
            try:
                self.browser.page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                self.logger.info("login: networkidle wait timed out, proceeding anyway")

            # Wait for the login form to render
            self.logger.info("Step 1: Waiting for email field")
            email_field = self.browser.page.wait_for_selector(self.LOGIN_EMAIL_SELECTOR, timeout=30000)
            email_field.fill(self.config.username)
            time.sleep(0.5)

            # Click submit to proceed to password
            submit_btn = self.browser.page.wait_for_selector(self.LOGIN_SUBMIT_SELECTOR, timeout=10000)
            submit_btn.click()
            time.sleep(2)

            # Step 2: Enter password
            self.logger.info("Step 2: Entering password")
            password_field = self.browser.page.wait_for_selector(self.LOGIN_PASSWORD_SELECTOR, timeout=30000)
            password_field.fill(self.config.password)
            time.sleep(0.5)

            # Click submit to complete login
            submit_btn = self.browser.page.wait_for_selector(self.LOGIN_SUBMIT_SELECTOR, timeout=10000)
            submit_btn.click()

            # Wait for URL to change away from login page (SPA redirect after auth)
            self.logger.info("Waiting for post-login navigation...")
            try:
                self.browser.page.wait_for_url(
                    lambda url: "/account/login" not in url,
                    timeout=self.SPA_TIMEOUT
                )
            except Exception as wait_err:
                self.logger.warning(f"wait_for_url timed out: {wait_err}")
                # Fall through to URL check below

            time.sleep(3)  # Extra settle time for SPA to finish loading

            # Verify login success by checking URL
            if "/account/login" in self.browser.page.url:
                self.logger.error("Login failed - still on login page")
                return False

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return False

    def extract_tickets(self) -> list[Ticket]:
        """Extract tickets from the Service Requests List view."""
        all_tickets = []

        try:
            self.logger.info("Navigating to Service Requests List")
            self._goto_spa(self.SERVICE_REQUESTS_LIST_URL)
            time.sleep(5)

            self.browser.page.wait_for_selector(self.LIST_PAGE_READY_SELECTOR, timeout=20000)

            # Default filter is "Closed Status is false" (mirrors the board's
            # exclude-Closed-column behavior). Drop any other filter (e.g. a
            # leftover queue/type filter) so every queue is visible.
            self._remove_non_default_filters()
            self._ensure_show_50_rows()

            list_entries = self._collect_list_tickets()
            self.logger.info(f"Found {len(list_entries)} tickets in list view")

            for i, entry in enumerate(list_entries):
                ticket_id = entry["ticket_id"]
                try:
                    # Known ticket: register presence only. Updates come from Znuny.
                    if self.is_known_ticket(ticket_id):
                        all_tickets.append(self.presence_ticket(ticket_id))
                        continue

                    self.logger.info(f"Processing new ticket {i+1}/{len(list_entries)}: {ticket_id}")
                    self._goto_spa(entry["portal_url"])
                    self.browser.page.wait_for_selector(self.TICKET_NUMBER_SELECTOR, timeout=15000)
                    time.sleep(1)

                    ticket = self._parse_ticket_detail_page("")
                    if ticket:
                        all_tickets.append(ticket)
                except Exception as e:
                    self.logger.warning(f"Error extracting ticket {ticket_id}: {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return all_tickets

    def _remove_non_default_filters(self):
        """Remove any filter other than 'Closed Status is false'.

        Ensures no queue/type filter (left over from a prior session or a
        saved view) hides tickets from other queues.
        """
        try:
            for _ in range(10):  # safety cap against unexpected re-render loops
                badges = self.browser.page.query_selector_all(self.FILTER_BADGE_SELECTOR)
                target = None
                for badge in badges:
                    text = (badge.text_content() or "")
                    if "closed status" not in text.lower():
                        target = badge
                        break
                if not target:
                    break
                delete_btn = target.query_selector(self.FILTER_DELETE_BUTTON_SELECTOR)
                if not delete_btn:
                    break
                delete_btn.click()
                time.sleep(1.5)
        except Exception as e:
            self.logger.warning(f"Error removing extra filters: {e}")

    def _ensure_show_50_rows(self):
        """Set the page size to 50 rows if it isn't already."""
        try:
            label = self.browser.page.query_selector(self.SHOWS_LABEL_SELECTOR)
            current = label.get_attribute("data-test") if label else None
            if current == "shows[50]":
                return

            toggle = self.browser.page.query_selector(self.ROWS_PER_PAGE_TOGGLE_SELECTOR)
            if not toggle:
                return
            toggle.click()
            time.sleep(0.5)

            items = self.browser.page.query_selector_all(self.ROWS_PER_PAGE_ITEM_SELECTOR)
            for item in items:
                text = (item.text_content() or "").strip()
                if text.replace(" ", "") == "50rows":
                    item.click()
                    time.sleep(1.5)
                    return
        except Exception as e:
            self.logger.warning(f"Error setting rows per page to 50: {e}")

    def _collect_list_tickets(self) -> list[dict]:
        """Walk every page of the list view, collecting ticket id + detail URL."""
        tickets = []
        seen_ids = set()

        for _ in range(100):  # safety cap against pagination loops
            try:
                self.browser.page.wait_for_selector(self.TICKET_LIST_CARD_SELECTOR, timeout=15000)
            except Exception:
                # No tickets on this page (e.g. zero open tickets)
                break

            cards = self.browser.page.query_selector_all(self.TICKET_LIST_CARD_SELECTOR)
            for card in cards:
                number_el = card.query_selector(self.TICKET_NUMBER_SELECTOR)
                ticket_id = (number_el.text_content() or "").strip() if number_el else None
                href = card.get_attribute("href")
                if not ticket_id or not href:
                    continue
                if ticket_id in seen_ids:
                    continue
                seen_ids.add(ticket_id)
                portal_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                tickets.append({"ticket_id": ticket_id, "portal_url": portal_url})

            next_link = self.browser.page.query_selector(self.NEXT_PAGE_SELECTOR)
            if not next_link:
                break
            next_link.click()
            time.sleep(2)

        return tickets

    def _parse_ticket_detail_page(self, board_status: str) -> Ticket | None:
        """Parse the ticket detail page and extract all information."""
        try:
            # Capture the current URL (contains UUID for this ticket)
            portal_url = self.browser.page.url

            # Ticket number
            ticket_id = self._get_element_text(self.TICKET_NUMBER_SELECTOR)
            if not ticket_id:
                self.logger.warning("Could not find ticket ID on detail page")
                return None

            # Status from badge
            status = self._get_element_text(self.TICKET_STATUS_BADGE_SELECTOR) or board_status

            # Contact information
            contact_name_raw = self._get_element_text(self.CONTACT_NAME_SELECTOR)
            contact_name = contact_name_raw
            account_number = None

            # Extract account number from parentheses, e.g., "Rise and Shine (7610023404100858)"
            if contact_name_raw and '(' in contact_name_raw:
                match = re.search(r'\((\d+)\)', contact_name_raw)
                if match:
                    account_number = match.group(1)
                contact_name = contact_name_raw.split('(')[0].strip()

            # Address - same building-code-only style the Medianet ticket formatter
            # displays (badge tag and neighborhood/area dropped, e.g.
            # "HOMEUD-05-02-06, Neighborhood 4" -> "UD-05-02-06").
            address_badge = self._get_element_text(self.CONTACT_ADDRESS_BADGE_SELECTOR) or ""  # HOME/WORK
            address_name = self._get_element_text(self.CONTACT_ADDRESS_NAME_SELECTOR) or ""
            address_location = self._get_element_text(self.CONTACT_ADDRESS_SELECTOR) or ""

            address_parts = []
            if address_name:
                address_parts.append(address_name.rstrip(', '))
            if address_location:
                address_parts.append(address_location)
            address_raw = f"{address_badge}{', '.join(address_parts)}" if address_parts else ""

            if not address_raw:
                address_raw = self._get_element_text(self.LOCATION_SELECTOR) or ""

            address = clean_building_code(address_raw) or None

            # Team
            team = self._get_element_text(self.TEAM_SELECTOR)

            # Queue/Type (ticket type)
            ticket_type = self._get_element_text(self.QUEUE_NAME_SELECTOR)

            # Priority
            priority = self._get_element_text(self.PRIORITY_SELECTOR)

            # Resolution status
            resolved_status = self._get_element_text(self.RESOLVED_STATUS_SELECTOR)
            if resolved_status:
                status = f"{status} - {resolved_status}"

            # Close date
            close_date_str = self._get_element_text(self.CLOSE_DATE_SELECTOR)
            ticket_time = self._parse_date(close_date_str)

            # Determine if ticket is completed (closed/resolved)
            is_completed = False
            completed_at = None
            if status and ("closed" in status.lower() or "resolved" in status.lower()):
                is_completed = True
                completed_at = ticket_time  # Use close date as completion time

            # Notes are intentionally not collected (updates come from Znuny)
            notes = None

            # Use extracted account number
            account = account_number

            return Ticket(
                portal="medianet",
                ticket_id=ticket_id,
                address=address,
                account=account,
                customer_name=contact_name,
                ticket_type=ticket_type,
                portal_created_at=ticket_time,
                service_type=team,  # Using team as service type
                status=status,
                kpi=priority,  # Using priority as KPI
                notes=notes,
                completed_at=completed_at,  # Set for closed/resolved tickets
                portal_url=portal_url  # Direct URL to ticket detail page
            )

        except Exception as e:
            self.logger.error(f"Error parsing ticket detail page: {e}")
            return None

    def _get_element_text(self, selector: str) -> str | None:
        """Get text from an element by CSS selector."""
        try:
            elements = self.browser.page.query_selector_all(selector)
            if elements:
                text = (elements[0].text_content() or "").strip()
                if not text:
                    # Try getting value attribute (for textarea/input)
                    text = elements[0].get_attribute('value')
                    if text:
                        text = text.strip()
                return text if text else None
        except Exception as e:
            self.logger.debug(f"Could not get element text for {selector}: {e}")
        return None

    def _extract_notes(self) -> str | None:
        """Extract notes from the Notes card on the detail page."""
        try:
            notes_content = self.browser.page.query_selector_all(self.NOTES_CONTENT_SELECTOR)
            if notes_content:
                text = (notes_content[0].text_content() or "").strip()
                # Check if it's the "No notes found" message
                if text and "No notes found" not in text:
                    # Remove the "NOTES" header if present
                    text = re.sub(r'^NOTES\s*', '', text, flags=re.IGNORECASE)
                    return text.strip() if text.strip() else None
        except Exception as e:
            self.logger.debug(f"Could not extract notes: {e}")
        return None

    def _parse_date(self, date_str: str | None) -> datetime | None:
        """Parse date string in various formats."""
        if not date_str:
            return None

        # Clean up the date string
        date_str = date_str.strip()

        # Try different date formats
        formats = [
            "%d %b %Y %H:%M",      # "28 Apr 2025 20:58"
            "%d %b  %Y %H:%M",     # "28 Apr  2025 20:58" (double space)
            "%d %b %Y",            # "28 Apr 2025"
            "%Y-%m-%d %H:%M:%S",   # "2025-04-28 20:58:00"
            "%Y-%m-%d",            # "2025-04-28"
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue

        self.logger.warning(f"Could not parse date: {date_str}")
        return None

    def logout(self):
        """Logout is not explicitly required for this portal."""
        self.logger.info("Medianet logout - closing browser session")

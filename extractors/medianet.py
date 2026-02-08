import time
import re
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from .base import BaseExtractor
from models.ticket import Ticket


class MedianetExtractor(BaseExtractor):
    """Extractor for Medianet CRM.COM portal.

    This portal uses a Kanban-style board with columns for different statuses.
    Tickets don't disappear when completed - they move to "Closed" status.
    """

    # ============================================================
    # CSS SELECTORS
    # ============================================================

    # Login page selectors (two-step login: email first, then password)
    LOGIN_EMAIL_SELECTOR = "input[type='email'], input[name='username']"
    LOGIN_PASSWORD_SELECTOR = "input[type='password']"
    LOGIN_SUBMIT_SELECTOR = "button[type='submit']"

    # Service Requests Board page
    SERVICE_REQUESTS_URL = "https://app.crm.com/crm/service-requests-board"

    # Board selectors
    BOARD_COLUMN_SELECTOR = ".board-column"
    COLUMN_TITLE_SELECTOR = ".card-title, .title"
    TICKET_CARD_SELECTOR = ".draggable"

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

    # React Select dropdown for ticket type filter
    TICKET_TYPE_DROPDOWN_SELECTOR = ".react-select__control"
    TICKET_TYPE_DROPDOWN_MENU_SELECTOR = ".react-select__menu"
    TICKET_TYPE_OPTION_SELECTOR = ".react-select__option"
    TICKET_TYPE_CURRENT_VALUE_SELECTOR = "[data-test='singleValue']"

    # ============================================================

    def is_logged_in(self) -> bool:
        """Check if currently logged in to Medianet portal."""
        try:
            # Check if browser is available
            if not self.browser or not self.browser.driver:
                self.logger.info("is_logged_in: No browser available")
                return False

            # Check current URL first before navigating
            try:
                current_url = self.browser.driver.current_url
                self.logger.info(f"is_logged_in: Current URL before check: {current_url}")
            except Exception as e:
                self.logger.info(f"is_logged_in: Browser not responsive: {e}")
                return False

            # Navigate to service requests page and check if we can access it
            self.logger.info(f"is_logged_in: Navigating to {self.SERVICE_REQUESTS_URL}")
            self.navigate_to(self.SERVICE_REQUESTS_URL)
            time.sleep(4)  # Increased wait time for page load

            # Check current URL after navigation
            current_url = self.browser.driver.current_url
            self.logger.info(f"is_logged_in: URL after navigation: {current_url}")

            # If redirected to login page, we're not logged in
            if "/account/login" in current_url:
                self.logger.info("is_logged_in: Redirected to login page - session expired")
                return False

            # Check if board columns are visible (indicates successful load)
            columns = self.browser.driver.find_elements(By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR)
            self.logger.info(f"is_logged_in: Found {len(columns)} board columns")

            if len(columns) > 0:
                self.logger.info("Session valid - board loaded successfully")
                return True

            # Maybe need more time for columns to load
            self.logger.info("is_logged_in: No columns found, waiting more...")
            time.sleep(3)
            columns = self.browser.driver.find_elements(By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR)
            self.logger.info(f"is_logged_in: After additional wait, found {len(columns)} columns")

            if len(columns) > 0:
                self.logger.info("Session valid - board loaded after extended wait")
                return True

            self.logger.info("is_logged_in: No board columns found - session may be invalid")
            return False
        except Exception as e:
            self.logger.warning(f"is_logged_in check failed with error: {e}")
            return False

    def login(self) -> bool:
        """Login to Medianet portal using two-step authentication."""
        self.logger.info(f"Logging into Medianet portal: {self.config.url}")

        try:
            self.navigate_to(self.config.url)
            time.sleep(3)

            current_url = self.browser.driver.current_url
            self.logger.info(f"login: Current URL after navigation: {current_url}")

            # Check if redirected to login page
            if "/account/login" not in current_url:
                self.logger.info("login: Session already active (not on login page) - reusing session")
                # This means is_logged_in() returned False but session is actually valid
                # This can happen if is_logged_in() had an issue checking the board
                return True

            # Step 1: Enter email
            self.logger.info("Step 1: Entering email")
            email_field = WebDriverWait(self.browser.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.LOGIN_EMAIL_SELECTOR))
            )
            email_field.clear()
            email_field.send_keys(self.config.username)
            time.sleep(0.5)

            # Click submit to proceed to password
            submit_btn = self.browser.driver.find_element(By.CSS_SELECTOR, self.LOGIN_SUBMIT_SELECTOR)
            submit_btn.click()
            time.sleep(2)

            # Step 2: Enter password
            self.logger.info("Step 2: Entering password")
            password_field = WebDriverWait(self.browser.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.LOGIN_PASSWORD_SELECTOR))
            )
            password_field.clear()
            password_field.send_keys(self.config.password)
            time.sleep(0.5)

            # Click submit to complete login
            submit_btn = self.browser.driver.find_element(By.CSS_SELECTOR, self.LOGIN_SUBMIT_SELECTOR)
            submit_btn.click()
            time.sleep(5)

            # Verify login success by checking URL
            if "/account/login" in self.browser.driver.current_url:
                self.logger.error("Login failed - still on login page")
                return False

            self.logger.info("Login successful")
            return True

        except Exception as e:
            self.logger.error(f"Login error: {e}")
            return False

    def extract_tickets(self) -> list[Ticket]:
        """Extract tickets from the Kanban board, iterating through all ticket types."""
        all_tickets = []
        seen_ticket_ids = set()  # Avoid duplicates across types

        try:
            # Navigate to Service Requests Board
            self.logger.info("Navigating to Service Requests Board")
            self.navigate_to(self.SERVICE_REQUESTS_URL)
            time.sleep(5)

            # Wait for board to load
            WebDriverWait(self.browser.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR))
            )

            # Get all available ticket types from the dropdown
            ticket_types = self._get_ticket_type_options()
            self.logger.info(f"Found {len(ticket_types)} ticket types: {ticket_types}")

            # Iterate through each ticket type
            for ticket_type in ticket_types:
                try:
                    # Navigate back to board page first
                    self.logger.info(f"Navigating to board for ticket type: {ticket_type}")
                    self.navigate_to(self.SERVICE_REQUESTS_URL)
                    time.sleep(3)

                    # Wait for board to load
                    WebDriverWait(self.browser.driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR))
                    )

                    # Select the ticket type
                    self._select_ticket_type(ticket_type)
                    time.sleep(3)

                    # Wait for board to refresh
                    WebDriverWait(self.browser.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR))
                    )

                    # Get all ticket cards from the board for this type
                    ticket_cards = self._get_all_ticket_cards()
                    self.logger.info(f"Found {len(ticket_cards)} tickets for type '{ticket_type}'")

                    # Extract details for each ticket
                    for i, card_info in enumerate(ticket_cards):
                        try:
                            ticket_id = card_info['ticket_id']

                            # Skip if we've already processed this ticket
                            if ticket_id in seen_ticket_ids:
                                self.logger.debug(f"Skipping duplicate ticket: {ticket_id}")
                                continue

                            self.logger.info(f"Processing ticket {i+1}/{len(ticket_cards)}: {ticket_id}")
                            ticket = self._extract_ticket_details(card_info)
                            if ticket:
                                all_tickets.append(ticket)
                                seen_ticket_ids.add(ticket_id)
                        except Exception as e:
                            self.logger.warning(f"Error extracting ticket {card_info.get('ticket_id', 'unknown')}: {e}")
                            continue

                except Exception as e:
                    self.logger.warning(f"Error processing ticket type '{ticket_type}': {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error extracting tickets: {e}")

        return all_tickets

    def _get_ticket_type_options(self) -> list[str]:
        """Get all available ticket type options from the React Select dropdown."""
        options = []
        try:
            # Click the dropdown to open it
            dropdown = self.browser.driver.find_element(By.CSS_SELECTOR, self.TICKET_TYPE_DROPDOWN_SELECTOR)
            dropdown.click()
            time.sleep(1)

            # Wait for menu to appear
            WebDriverWait(self.browser.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.TICKET_TYPE_DROPDOWN_MENU_SELECTOR))
            )

            # Get all options
            option_elements = self.browser.driver.find_elements(By.CSS_SELECTOR, self.TICKET_TYPE_OPTION_SELECTOR)
            for opt in option_elements:
                text = opt.text.strip()
                # Skip placeholder options
                if text and not text.lower().startswith("select"):
                    options.append(text)

            # Close dropdown by pressing Escape or clicking elsewhere
            from selenium.webdriver.common.keys import Keys
            self.browser.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.5)

        except Exception as e:
            self.logger.warning(f"Error getting ticket type options: {e}")
            # If we can't get options, try to get current value at least
            try:
                current_value = self.browser.driver.find_element(By.CSS_SELECTOR, self.TICKET_TYPE_CURRENT_VALUE_SELECTOR)
                if current_value and current_value.text.strip():
                    options.append(current_value.text.strip())
            except Exception:
                pass

        return options if options else ["Customer Inquiry"]  # Default fallback

    def _select_ticket_type(self, ticket_type: str):
        """Select a ticket type from the React Select dropdown."""
        try:
            # Click the dropdown to open it
            dropdown = self.browser.driver.find_element(By.CSS_SELECTOR, self.TICKET_TYPE_DROPDOWN_SELECTOR)
            dropdown.click()
            time.sleep(1)

            # Wait for menu to appear
            WebDriverWait(self.browser.driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, self.TICKET_TYPE_DROPDOWN_MENU_SELECTOR))
            )

            # Find and click the option
            option_elements = self.browser.driver.find_elements(By.CSS_SELECTOR, self.TICKET_TYPE_OPTION_SELECTOR)
            for opt in option_elements:
                if opt.text.strip() == ticket_type:
                    opt.click()
                    time.sleep(1)
                    return

            # If exact match not found, close dropdown
            self.browser.driver.find_element(By.TAG_NAME, "body").click()

        except Exception as e:
            self.logger.warning(f"Error selecting ticket type '{ticket_type}': {e}")

    def _get_all_ticket_cards(self) -> list[dict]:
        """Get all ticket cards from the first 2 board columns (skip Closed column)."""
        ticket_cards = []

        try:
            columns = self.browser.driver.find_elements(By.CSS_SELECTOR, self.BOARD_COLUMN_SELECTOR)
            self.logger.info(f"Found {len(columns)} board columns")

            # Only process first 2 columns (skip Closed which is usually column 3)
            for col_idx, col in enumerate(columns):
                try:
                    # Get column status
                    title_el = col.find_elements(By.CSS_SELECTOR, self.COLUMN_TITLE_SELECTOR)
                    column_status = title_el[0].text.split('\n')[0] if title_el else "Unknown"

                    # Skip Closed column
                    if "closed" in column_status.lower():
                        self.logger.info(f"Skipping Closed column: '{column_status}'")
                        continue

                    # Get tickets in this column
                    cards = col.find_elements(By.CSS_SELECTOR, self.TICKET_CARD_SELECTOR)
                    self.logger.info(f"Column '{column_status}': {len(cards)} tickets")

                    for card in cards:
                        try:
                            # Extract ticket ID from card using data-test attribute
                            ticket_id_el = card.find_elements(By.CSS_SELECTOR, "span[data-test='serviceRequestNumber']")
                            ticket_id = ticket_id_el[0].text.strip() if ticket_id_el else None

                            if not ticket_id:
                                # Fallback: try getting from card text
                                card_text = card.text.strip()
                                lines = card_text.split('\n')
                                ticket_id = lines[0] if lines else None

                            if ticket_id:
                                ticket_cards.append({
                                    'ticket_id': ticket_id,
                                    'column_status': column_status,
                                    'element': card
                                })
                        except Exception as e:
                            self.logger.warning(f"Error parsing ticket card: {e}")

                except Exception as e:
                    self.logger.warning(f"Error processing column: {e}")

        except Exception as e:
            self.logger.error(f"Error getting ticket cards: {e}")

        return ticket_cards

    def _extract_ticket_details(self, card_info: dict) -> Ticket | None:
        """Click on a ticket card and extract details from the detail page."""
        try:
            # Navigate back to board if needed
            if "/service-requests-board" not in self.browser.driver.current_url:
                self.navigate_to(self.SERVICE_REQUESTS_URL)
                time.sleep(3)

                # Re-find the ticket card
                WebDriverWait(self.browser.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, self.TICKET_CARD_SELECTOR))
                )

            # Find and click the ticket card
            ticket_id = card_info['ticket_id']
            cards = self.browser.driver.find_elements(By.CSS_SELECTOR, self.TICKET_CARD_SELECTOR)
            target_card = None

            for card in cards:
                if ticket_id in card.text:
                    target_card = card
                    break

            if not target_card:
                self.logger.warning(f"Could not find ticket card for {ticket_id}")
                return None

            # Click to open detail page
            target_card.click()
            time.sleep(3)

            # Wait for detail page to load
            WebDriverWait(self.browser.driver, 10).until(
                EC.url_contains("/service-request/")
            )
            time.sleep(2)

            # Extract ticket details
            return self._parse_ticket_detail_page(card_info['column_status'])

        except Exception as e:
            self.logger.error(f"Error extracting ticket details: {e}")
            return None

    def _parse_ticket_detail_page(self, board_status: str) -> Ticket | None:
        """Parse the ticket detail page and extract all information."""
        try:
            # Capture the current URL (contains UUID for this ticket)
            portal_url = self.browser.driver.current_url

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

            phone = self._get_element_text(self.CONTACT_PHONE_SELECTOR)
            email = self._get_element_text(self.CONTACT_EMAIL_SELECTOR)

            # Address - combine all parts
            address_badge = self._get_element_text(self.CONTACT_ADDRESS_BADGE_SELECTOR) or ""  # HOME/WORK
            address_name = self._get_element_text(self.CONTACT_ADDRESS_NAME_SELECTOR) or ""
            address_location = self._get_element_text(self.CONTACT_ADDRESS_SELECTOR) or ""

            # Build full address
            address_parts = []
            if address_name:
                address_parts.append(address_name.rstrip(', '))
            if address_location:
                address_parts.append(address_location)

            address = ", ".join(address_parts) if address_parts else None

            # Add address type badge if present
            if address_badge and address:
                address = f"[{address_badge}] {address}"

            # Location (where field) - use as fallback or additional info
            location = self._get_element_text(self.LOCATION_SELECTOR)
            if location:
                if not address:
                    address = location
                elif location not in address:
                    address = f"{address} | Where: {location}"

            # Description (notes/description field)
            description = self._get_element_text(self.DESCRIPTION_SELECTOR)

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

            # Notes from Notes card
            notes = self._extract_notes()

            # If description exists and no notes, use description as notes
            if description and not notes:
                notes = description
            elif description and notes:
                notes = f"Description: {description}\n---\n{notes}"

            # Use extracted account number, append phone/email to notes if present
            account = account_number

            # Add contact info to notes if available
            contact_info_parts = []
            if phone:
                contact_info_parts.append(f"Phone: +960 {phone}")
            if email:
                contact_info_parts.append(f"Email: {email}")
            if contact_info_parts:
                contact_info = " | ".join(contact_info_parts)
                if notes:
                    notes = f"{notes}\n---\nContact: {contact_info}"
                else:
                    notes = f"Contact: {contact_info}"

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
            elements = self.browser.driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                text = elements[0].text.strip()
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
            notes_content = self.browser.driver.find_elements(By.CSS_SELECTOR, self.NOTES_CONTENT_SELECTOR)
            if notes_content:
                text = notes_content[0].text.strip()
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

"""
Znuny client using Selenium for web-based ticket search.
Searches tickets by subject/title using *ticketid* pattern.
Fetches ticket details including creator, creation time, and article history.
"""

import time
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field

# Maldives timezone (UTC+5)
MALDIVES_TZ = timezone(timedelta(hours=5))
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from config import Config
from utils.logger import get_logger

logger = get_logger("znuny")


@dataclass
class ZnunyArticle:
    """Represents an article/note in a Znuny ticket."""
    article_number: int
    sender: str  # Customer/caller for Phone, staff for Internal
    via: str  # Phone, Internal, Email, etc.
    subject: str
    created_at: datetime | None
    created_at_str: str
    created_by: str = ""  # Staff who created the article (from "by X" in detail)
    body: str = ""  # The actual note/article content


@dataclass
class ZnunyTicketDetails:
    """Details fetched from a Znuny ticket."""
    ticket_number: str
    created_at: datetime | None
    created_at_str: str
    created_by: str
    owner: str
    state: str
    address: str = ""  # Address from phone ticket or first article
    znuny_url: str = ""  # Direct URL to ticket in Znuny
    articles: list[ZnunyArticle] = field(default_factory=list)


class ZnunyClient:
    """Client for searching tickets in Znuny using Selenium."""

    # Znuny URLs
    BASE_URL = "https://10.241.1.110"
    LOGIN_URL = f"{BASE_URL}/otrs/index.pl"
    DASHBOARD_URL = f"{BASE_URL}/otrs/index.pl?Action=AgentDashboard"
    SEARCH_URL = f"{BASE_URL}/otrs/index.pl?Action=AgentTicketSearch"

    # CSS Selectors
    LOGIN_USER_SELECTOR = "input[name='User']"
    LOGIN_PASSWORD_SELECTOR = "input[name='Password']"
    LOGIN_BUTTON_SELECTOR = "#LoginButton"

    # Search form selectors
    SEARCH_TITLE_SELECTOR = "input[name='Title']"
    SEARCH_SUBMIT_SELECTOR = "#SearchFormSubmit"

    # Results selectors
    TICKET_LINK_SELECTOR = "a[href*='AgentTicketZoom']"
    TICKET_TITLE_SELECTOR = ".MasterActionLink"

    # Class-level browser instance for session persistence
    _shared_driver = None
    _shared_logged_in = False

    def __init__(self):
        self.username = Config.ZNUNY_USERNAME
        self.password = Config.ZNUNY_PASSWORD
        self._open_tickets_cache = None  # Cache for open tickets

    @property
    def driver(self):
        """Get shared driver instance."""
        return ZnunyClient._shared_driver

    @driver.setter
    def driver(self, value):
        """Set shared driver instance."""
        ZnunyClient._shared_driver = value

    @property
    def _logged_in(self):
        """Get shared login state."""
        return ZnunyClient._shared_logged_in

    @_logged_in.setter
    def _logged_in(self, value):
        """Set shared login state."""
        ZnunyClient._shared_logged_in = value

    def _setup_browser(self):
        """Setup Chrome browser with options, reusing existing session if available."""
        if self.driver:
            # Check if browser is still alive
            try:
                self.driver.current_url
                logger.info("Reusing existing Znuny browser session")
                return
            except:
                logger.info("Znuny browser session died, creating new one")
                self.driver = None
                self._logged_in = False

        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--ignore-ssl-errors")

        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=options)
        self.driver.implicitly_wait(10)
        logger.info("Znuny browser started (new session)")

    def _login(self) -> bool:
        """Login to Znuny, reusing existing session if valid."""
        self._setup_browser()

        if self._logged_in:
            # Check if still logged in
            try:
                self.driver.get(self.DASHBOARD_URL)
                time.sleep(2)
                if "Dashboard" in self.driver.title:
                    logger.info("Znuny session still active, skipping login")
                    return True
            except:
                pass
            self._logged_in = False
            logger.info("Znuny session expired, re-logging in...")

        try:
            self.driver.get(self.LOGIN_URL)
            time.sleep(2)

            # Check if already on dashboard (session still valid from cookies)
            if "Dashboard" in self.driver.title:
                self._logged_in = True
                logger.info("Znuny session valid from cookies")
                return True

            # Enter credentials
            user_field = self.driver.find_element(By.CSS_SELECTOR, self.LOGIN_USER_SELECTOR)
            user_field.clear()
            user_field.send_keys(self.username)

            password_field = self.driver.find_element(By.CSS_SELECTOR, self.LOGIN_PASSWORD_SELECTOR)
            password_field.clear()
            password_field.send_keys(self.password)

            # Click login
            login_btn = self.driver.find_element(By.CSS_SELECTOR, self.LOGIN_BUTTON_SELECTOR)
            login_btn.click()
            time.sleep(3)

            # Verify login
            if "Dashboard" in self.driver.title:
                self._logged_in = True
                logger.info("Znuny login successful (new session)")
                return True
            else:
                logger.error("Znuny login failed")
                return False

        except Exception as e:
            logger.error(f"Znuny login error: {e}")
            return False

    def get_open_tickets(self, force_refresh: bool = False) -> list[dict]:
        """
        Get all open tickets from the dashboard widget.
        Returns list of tickets with ticket_number and title.
        Handles pagination to get all tickets.
        Uses cache to avoid repeated fetches.
        """
        # Return cached results if available
        if self._open_tickets_cache is not None and not force_refresh:
            return self._open_tickets_cache

        if not self._login():
            return []

        try:
            self.driver.get(self.DASHBOARD_URL)
            time.sleep(2)

            all_tickets = []
            page = 1
            max_pages = 10  # Safety limit

            while page <= max_pages:
                # Get tickets from current page of the Open Tickets widget
                rows = self.driver.find_elements(By.CSS_SELECTOR, "#Dashboard0130-TicketOpen tr.MasterAction")

                for row in rows:
                    try:
                        # Get ticket number from MasterActionLink
                        link = row.find_element(By.CSS_SELECTOR, "a.MasterActionLink")
                        ticket_number = link.text.strip()
                        href = link.get_attribute('href') or ""

                        # Get title from the last td div
                        title_divs = row.find_elements(By.CSS_SELECTOR, "td div[title]")
                        title = ""
                        if title_divs:
                            title = title_divs[-1].get_attribute('title') or title_divs[-1].text

                        if ticket_number:
                            all_tickets.append({
                                "ticket_number": ticket_number,
                                "title": title,
                                "href": href
                            })
                    except:
                        continue

                # Check for next page
                next_page = page + 1
                next_page_link = self.driver.find_elements(
                    By.CSS_SELECTOR, f"#Dashboard0130-TicketOpenPage{next_page}"
                )

                if next_page_link and "Selected" not in next_page_link[0].get_attribute("class"):
                    next_page_link[0].click()
                    time.sleep(2)
                    page += 1
                else:
                    break

            logger.info(f"Znuny dashboard: found {len(all_tickets)} open tickets")
            self._open_tickets_cache = all_tickets
            return all_tickets

        except Exception as e:
            logger.error(f"Znuny get_open_tickets error: {e}")
            return []

    def clear_cache(self):
        """Clear the open tickets cache."""
        self._open_tickets_cache = None

    def search_by_title(self, search_term: str) -> list[dict]:
        """
        Search for tickets by checking if search_term appears in any open ticket's title.
        Uses cached dashboard data for efficiency.
        Returns list of matching tickets with ticket_number and title.
        """
        # Get all open tickets from dashboard (uses cache)
        all_tickets = self.get_open_tickets()

        if not all_tickets:
            logger.info(f"Znuny search for '{search_term}': no open tickets found")
            return []

        # Filter tickets that contain the search term in the title
        search_lower = search_term.lower()
        matching = [
            t for t in all_tickets
            if search_lower in t.get("title", "").lower()
        ]

        logger.info(f"Znuny search for '{search_term}': found {len(matching)} tickets")
        return matching

    def get_ticket_details(self, ticket_number: str) -> ZnunyTicketDetails | None:
        """
        Fetch detailed information about a Znuny ticket.
        Returns ticket details including creation time, creator, and all articles.
        """
        if not self._login():
            return None

        try:
            # Find the ticket in cache to get its URL
            all_tickets = self.get_open_tickets()
            ticket_info = next((t for t in all_tickets if t["ticket_number"] == ticket_number), None)

            if not ticket_info or not ticket_info.get("href"):
                logger.warning(f"Ticket {ticket_number} not found in open tickets")
                return None

            # Navigate to ticket detail page
            self.driver.get(ticket_info["href"])
            time.sleep(2)

            # Capture the ticket URL
            znuny_url = self.driver.current_url

            # Parse ticket details from sidebar
            created_at = None
            created_at_str = ""
            created_by = ""
            owner = ""
            state = ""

            # Get sidebar content
            sidebar = self.driver.find_elements(By.CSS_SELECTOR, ".SidebarColumn")
            if sidebar:
                sidebar_text = sidebar[0].text

                # Extract "Created:" line - format: "02/04/2026 11:32 (Indian/Maldives)"
                created_match = re.search(r"Created:\s*\n?(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", sidebar_text)
                if created_match:
                    created_at_str = created_match.group(1)
                    try:
                        created_at = datetime.strptime(created_at_str, "%m/%d/%Y %H:%M")
                    except ValueError:
                        pass

                # Extract "Created by:" line
                created_by_match = re.search(r"Created by:\s*\n?([^\n]+)", sidebar_text)
                if created_by_match:
                    created_by = created_by_match.group(1).strip()

                # Extract "Owner:" line
                owner_match = re.search(r"Owner:\s*\n?([^\n]+)", sidebar_text)
                if owner_match:
                    owner = owner_match.group(1).strip()

                # Extract "State:" line
                state_match = re.search(r"State:\s*\n?([^\n]+)", sidebar_text)
                if state_match:
                    state = state_match.group(1).strip()

            # Parse articles from article overview table
            articles = []
            article_rows = self.driver.find_elements(By.CSS_SELECTOR, ".WidgetSimple table tbody tr")

            # First pass: collect basic article info
            article_data = []
            for row in article_rows:
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) >= 7:
                        article_num_text = cells[0].text.strip()
                        if not article_num_text.isdigit():
                            continue

                        article_data.append({
                            "num": int(article_num_text),
                            "sender": cells[3].text.strip(),
                            "via": cells[4].text.strip(),
                            "subject": cells[5].text.strip(),
                            "created_str": cells[6].text.strip(),
                            "row": row
                        })
                except:
                    continue

            # Second pass: click each article to get the "by [Staff]" info and body content
            for data in article_data:
                try:
                    # Click on the article row to open detail
                    data["row"].click()
                    time.sleep(0.5)

                    # Look for the article header with "by [Staff Name]"
                    created_by = ""
                    article_headers = self.driver.find_elements(By.CSS_SELECTOR, ".WidgetSimple h2")
                    for header in article_headers:
                        header_text = header.text
                        # Look for "by [Name]" at the end of the header
                        by_match = re.search(r'\bby\s+([A-Za-z][A-Za-z\s]+)$', header_text, re.MULTILINE)
                        if by_match:
                            created_by = by_match.group(1).strip()
                            break

                    # If no "by" found, use sender for Internal articles
                    if not created_by and data["via"] == "Internal":
                        created_by = data["sender"]

                    # Get the article body content from iframe
                    body = ""
                    try:
                        # Znuny renders article content in an iframe with ID like "Iframe{article_id}"
                        # First, find the iframe in the article content area
                        iframes = self.driver.find_elements(By.CSS_SELECTOR, ".ArticleMailContentHTMLWrapper iframe, .ArticleMailContent iframe, iframe[id^='Iframe']")
                        if iframes:
                            # Switch to the iframe to get its content
                            self.driver.switch_to.frame(iframes[0])
                            try:
                                # Get the body text from inside the iframe
                                body_elem = self.driver.find_element(By.TAG_NAME, "body")
                                body = body_elem.text.strip()
                            finally:
                                # Always switch back to main content
                                self.driver.switch_to.default_content()

                        # Fallback: try direct selectors if iframe didn't work
                        if not body:
                            body_elements = self.driver.find_elements(By.CSS_SELECTOR, ".ArticleBody, .MessageBody")
                            if body_elements:
                                body = body_elements[0].text.strip()
                    except Exception as e:
                        logger.debug(f"Error extracting article body: {e}")
                        # Make sure we're back to main content
                        try:
                            self.driver.switch_to.default_content()
                        except:
                            pass

                    # Parse article created time
                    article_created = None
                    time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                    if time_match:
                        try:
                            article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M")
                        except ValueError:
                            pass

                    articles.append(ZnunyArticle(
                        article_number=data["num"],
                        sender=data["sender"],
                        via=data["via"],
                        subject=data["subject"],
                        created_at=article_created,
                        created_at_str=data["created_str"],
                        created_by=created_by,
                        body=body
                    ))
                except Exception as e:
                    logger.debug(f"Error parsing article {data.get('num', '?')}: {e}")
                    continue

            # Sort articles by number (newest first - descending)
            articles.sort(key=lambda a: a.article_number, reverse=True)

            # Extract address from first phone article body
            address = ""
            for article in articles:
                if article.via == "Phone" and article.body:
                    # Look for address patterns in the body
                    # Common patterns: "Address: ...", "Location: ...", or multi-line address
                    body_lines = article.body.split('\n')
                    for i, line in enumerate(body_lines):
                        line_lower = line.lower().strip()
                        if line_lower.startswith('address:') or line_lower.startswith('location:'):
                            # Get the address value after the label
                            addr = line.split(':', 1)[1].strip() if ':' in line else ''
                            if addr:
                                address = addr
                                break
                        # Also check for standalone address-like content (contains street indicators)
                        elif any(indicator in line_lower for indicator in ['flat', 'floor', 'building', 'street', 'road', 'lane', 'magu', 'hingun']):
                            address = line.strip()
                            break
                    if address:
                        break

            logger.info(f"Fetched details for ticket {ticket_number}: created by {created_by}, {len(articles)} articles, address={address[:30] if address else 'none'}")

            return ZnunyTicketDetails(
                ticket_number=ticket_number,
                created_at=created_at,
                created_at_str=created_at_str,
                created_by=created_by,
                owner=owner,
                state=state,
                address=address,
                znuny_url=znuny_url,
                articles=articles
            )

        except Exception as e:
            logger.error(f"Error fetching ticket details for {ticket_number}: {e}")
            return None

    def get_all_ticket_details(self) -> list[ZnunyTicketDetails]:
        """
        Fetch details for all open tickets.
        Returns list of ticket details with creator and article information.
        """
        all_tickets = self.get_open_tickets()
        details = []

        for ticket in all_tickets:
            ticket_number = ticket.get("ticket_number")
            if ticket_number:
                detail = self.get_ticket_details(ticket_number)
                if detail:
                    details.append(detail)

        logger.info(f"Fetched details for {len(details)} tickets")
        return details

    async def check_ticket_in_znuny(self, portal_ticket_id: str, customer_name: str = None) -> tuple[bool, str | None]:
        """
        Check if a portal ticket exists in Znuny by searching subject.
        Returns: (exists: bool, znuny_ticket_id: str | None)
        """
        results = self.search_by_title(portal_ticket_id)

        if results:
            znuny_id = results[0].get("ticket_number")
            return True, znuny_id

        return False, None

    def check_ticket_sync(self, portal_ticket_id: str) -> tuple[bool, str | None]:
        """
        Synchronous version of check_ticket_in_znuny.
        """
        results = self.search_by_title(portal_ticket_id)

        if results:
            znuny_id = results[0].get("ticket_number")
            return True, znuny_id

        return False, None

    def close(self, force: bool = False):
        """
        Close the browser session.
        By default, keeps session alive for reuse. Use force=True to actually close.
        """
        if not force:
            # Don't close by default - keep session for reuse
            logger.debug("Znuny close called but keeping session alive for reuse")
            return

        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            ZnunyClient._shared_driver = None
            ZnunyClient._shared_logged_in = False
            logger.info("Znuny browser closed (forced)")

    @classmethod
    def force_close(cls):
        """Force close the shared browser session."""
        if cls._shared_driver:
            try:
                cls._shared_driver.quit()
            except:
                pass
            cls._shared_driver = None
            cls._shared_logged_in = False
            logger.info("Znuny shared browser session closed")

    def __del__(self):
        # Don't close on garbage collection - keep session alive
        pass


# Synchronous wrapper for backward compatibility
class ZnunyClientSync:
    """Synchronous Znuny client."""

    def __init__(self):
        self._client = None

    def _get_client(self) -> ZnunyClient:
        if not self._client:
            self._client = ZnunyClient()
        return self._client

    def check_ticket_in_znuny(self, portal_ticket_id: str, customer_name: str = None) -> tuple[bool, str | None]:
        """Check if ticket exists in Znuny."""
        return self._get_client().check_ticket_sync(portal_ticket_id)

    def close(self, force: bool = False):
        """
        Close the client. By default keeps session alive for reuse.
        Use force=True to actually close the shared browser.
        """
        if self._client:
            self._client.close(force=force)
            self._client = None

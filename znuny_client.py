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

# Cache TTL in seconds (5 minutes)
CACHE_TTL_SECONDS = 300


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


@dataclass
class SiteVisit:
    """Represents a parsed OAN Site Visit from a Znuny article."""
    znuny_ticket_id: str
    article_number: int
    article_created_at: datetime | None
    site_type: str = ""
    service_provider: str = ""
    scheduled_time: str = ""  # HHMM or "now"
    assigned_to: str = ""
    visit_date: str = ""  # Date of the visit (from article date)


def parse_site_visit_article(article: ZnunyArticle, znuny_ticket_id: str) -> SiteVisit | None:
    """
    Parse an OAN Site Visit Arranged article to extract visit details.

    Expected format in article body:
    Site Type:  Fault ( no BB)
    Service Provider: ooredoo
    Time: 1130
    Assigned to: @maah
    """
    # Check if this is a site visit article
    if "OAN Site Visit Arranged" not in article.subject and "Preventative Maintenance - Site Visit" not in article.subject:
        return None

    body = article.body or ""

    # Parse fields from body
    site_type = ""
    service_provider = ""
    scheduled_time = ""
    assigned_to = ""

    # Site Type
    site_match = re.search(r"Site Type:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if site_match:
        site_type = site_match.group(1).strip()

    # Service Provider
    provider_match = re.search(r"Service Provider:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if provider_match:
        service_provider = provider_match.group(1).strip()

    # Time - can be HHMM format or "now" (now = article creation time)
    time_match = re.search(r"Time:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if time_match:
        raw_time = time_match.group(1).strip().lower()
        if raw_time in ("now", "nnow") and article.created_at:
            scheduled_time = article.created_at.strftime("%H:%M")
        else:
            scheduled_time = time_match.group(1).strip()

    # Assigned to - usually starts with @
    assigned_match = re.search(r"Assigned to:\s*@?(.+?)(?:\n|$)", body, re.IGNORECASE)
    if assigned_match:
        assigned_to = assigned_match.group(1).strip()

    # Get visit date from article creation date
    visit_date = ""
    if article.created_at:
        visit_date = article.created_at.strftime("%Y-%m-%d")

    return SiteVisit(
        znuny_ticket_id=znuny_ticket_id,
        article_number=article.article_number,
        article_created_at=article.created_at,
        site_type=site_type,
        service_provider=service_provider,
        scheduled_time=scheduled_time,
        assigned_to=assigned_to,
        visit_date=visit_date
    )


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
        self._cache_timestamp = None  # When cache was last refreshed
        self._ticket_details_cache = {}  # Cache for ticket details {ticket_number: (details, timestamp)}

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

    def _is_cache_valid(self) -> bool:
        """Check if cache is still valid based on TTL."""
        if self._open_tickets_cache is None or self._cache_timestamp is None:
            return False
        age = time.time() - self._cache_timestamp
        return age < CACHE_TTL_SECONDS

    def get_open_tickets(self, force_refresh: bool = False) -> list[dict]:
        """
        Get all open tickets from the dashboard widget.
        Returns list of tickets with ticket_number and title.
        Handles pagination to get all tickets.
        Uses TTL-based cache (5 min) to avoid repeated fetches.
        """
        # Return cached results if valid (TTL-based)
        if not force_refresh and self._is_cache_valid():
            logger.debug(f"Using cached open tickets (age: {int(time.time() - self._cache_timestamp)}s)")
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
            self._cache_timestamp = time.time()
            return all_tickets

        except Exception as e:
            logger.error(f"Znuny get_open_tickets error: {e}")
            return []

    def clear_cache(self):
        """Clear all caches."""
        self._open_tickets_cache = None
        self._cache_timestamp = None
        self._ticket_details_cache = {}

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

    def get_ticket_details(self, ticket_number: str, skip_body_fetch: bool = False) -> ZnunyTicketDetails | None:
        """
        Fetch detailed information about a Znuny ticket.
        Returns ticket details including creation time, creator, and all articles.

        OPTIMIZED: Only clicks articles that need body content (site visit articles with
        "OAN Site Visit" in subject). Other articles use basic info from table.

        Args:
            ticket_number: The Znuny ticket number
            skip_body_fetch: If True, skip fetching article bodies entirely (fastest mode)
        """
        # Check details cache first
        if ticket_number in self._ticket_details_cache:
            details, cache_time = self._ticket_details_cache[ticket_number]
            if time.time() - cache_time < CACHE_TTL_SECONDS:
                logger.debug(f"Using cached details for ticket {ticket_number}")
                return details

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
            time.sleep(1.5)  # Reduced from 2s

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
                        created_at = datetime.strptime(created_at_str, "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
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

            # Collect basic article info from table (no clicking needed)
            article_data = []
            for row in article_rows:
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) >= 7:
                        article_num_text = cells[0].text.strip()
                        if not article_num_text.isdigit():
                            continue

                        subject = cells[5].text.strip()
                        article_data.append({
                            "num": int(article_num_text),
                            "sender": cells[3].text.strip(),
                            "via": cells[4].text.strip(),
                            "subject": subject,
                            "created_str": cells[6].text.strip(),
                            "row": row,
                            # Only need body for site visit articles or Phone articles (for address)
                            "needs_body": "site visit" in subject.lower() or "preventative maintenance" in subject.lower() or cells[4].text.strip() == "Phone"
                        })
                except:
                    continue

            # Process articles - only click ones that need body content
            articles_needing_body = [d for d in article_data if d["needs_body"] and not skip_body_fetch]
            articles_skipped = [d for d in article_data if not d["needs_body"] or skip_body_fetch]

            # First, add articles that don't need body (fast - no clicking)
            for data in articles_skipped:
                article_created = None
                time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                if time_match:
                    try:
                        article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                    except ValueError:
                        pass

                # For Internal articles, use sender as created_by
                article_created_by = data["sender"] if data["via"] == "Internal" else ""

                articles.append(ZnunyArticle(
                    article_number=data["num"],
                    sender=data["sender"],
                    via=data["via"],
                    subject=data["subject"],
                    created_at=article_created,
                    created_at_str=data["created_str"],
                    created_by=article_created_by,
                    body=""  # No body needed for these
                ))

            # Then click only articles that need body content (site visits + first Phone)
            phone_article_processed = False
            for data in articles_needing_body:
                # Only process first Phone article (for address extraction)
                if data["via"] == "Phone" and phone_article_processed:
                    # Add without body
                    article_created = None
                    time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                    if time_match:
                        try:
                            article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                        except ValueError:
                            pass
                    articles.append(ZnunyArticle(
                        article_number=data["num"],
                        sender=data["sender"],
                        via=data["via"],
                        subject=data["subject"],
                        created_at=article_created,
                        created_at_str=data["created_str"],
                        created_by="",
                        body=""
                    ))
                    continue

                try:
                    # Click on the article row to open detail
                    data["row"].click()
                    time.sleep(0.3)  # Reduced from 0.5s

                    # Look for the article header with "by [Staff Name]"
                    article_created_by = ""
                    article_headers = self.driver.find_elements(By.CSS_SELECTOR, ".WidgetSimple h2")
                    for header in article_headers:
                        header_text = header.text
                        by_match = re.search(r'\bby\s+([A-Za-z][A-Za-z\s]+)$', header_text, re.MULTILINE)
                        if by_match:
                            article_created_by = by_match.group(1).strip()
                            break

                    if not article_created_by and data["via"] == "Internal":
                        article_created_by = data["sender"]

                    # Get the article body content from iframe
                    body = ""
                    try:
                        iframes = self.driver.find_elements(By.CSS_SELECTOR, ".ArticleMailContentHTMLWrapper iframe, .ArticleMailContent iframe, iframe[id^='Iframe']")
                        if iframes:
                            self.driver.switch_to.frame(iframes[0])
                            try:
                                body_elem = self.driver.find_element(By.TAG_NAME, "body")
                                body = body_elem.text.strip()
                            finally:
                                self.driver.switch_to.default_content()

                        if not body:
                            body_elements = self.driver.find_elements(By.CSS_SELECTOR, ".ArticleBody, .MessageBody")
                            if body_elements:
                                body = body_elements[0].text.strip()
                    except Exception as e:
                        logger.debug(f"Error extracting article body: {e}")
                        try:
                            self.driver.switch_to.default_content()
                        except:
                            pass

                    article_created = None
                    time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                    if time_match:
                        try:
                            article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                        except ValueError:
                            pass

                    articles.append(ZnunyArticle(
                        article_number=data["num"],
                        sender=data["sender"],
                        via=data["via"],
                        subject=data["subject"],
                        created_at=article_created,
                        created_at_str=data["created_str"],
                        created_by=article_created_by,
                        body=body
                    ))

                    if data["via"] == "Phone":
                        phone_article_processed = True

                except Exception as e:
                    logger.debug(f"Error parsing article {data.get('num', '?')}: {e}")
                    continue

            # Sort articles by number (newest first - descending)
            articles.sort(key=lambda a: a.article_number, reverse=True)

            # Extract address from first phone article body
            address = ""
            for article in articles:
                if article.via == "Phone" and article.body:
                    body_lines = article.body.split('\n')
                    for line in body_lines:
                        line_lower = line.lower().strip()
                        if line_lower.startswith('address:') or line_lower.startswith('location:'):
                            addr = line.split(':', 1)[1].strip() if ':' in line else ''
                            if addr:
                                address = addr
                                break
                        elif any(indicator in line_lower for indicator in ['flat', 'floor', 'building', 'street', 'road', 'lane', 'magu', 'hingun']):
                            address = line.strip()
                            break
                    if address:
                        break

            clicks_made = len([d for d in articles_needing_body if not (d["via"] == "Phone" and phone_article_processed)])
            logger.info(f"Fetched details for ticket {ticket_number}: created by {created_by}, {len(articles)} articles ({clicks_made} clicked), address={address[:30] if address else 'none'}")

            details = ZnunyTicketDetails(
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

            # Cache the details
            self._ticket_details_cache[ticket_number] = (details, time.time())

            return details

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

    def get_site_visit_tickets(self) -> list[dict]:
        """
        Get all tickets that have OAN Site Visit articles.
        Searches for tickets with "OAN Site Visit" in the title.
        Returns list of tickets with their details.
        """
        all_tickets = self.get_open_tickets()

        if not all_tickets:
            return []

        # Filter for site visit tickets
        site_visit_tickets = [
            t for t in all_tickets
            if "site visit" in t.get("title", "").lower()
        ]

        logger.info(f"Found {len(site_visit_tickets)} site visit tickets")
        return site_visit_tickets

    def extract_isp_ticket_id_from_title(self, title: str) -> dict:
        """
        Extract ISP portal ticket ID from a Znuny ticket title.
        Returns dict with portal and ticket_id if found.

        Common title formats:
        - "Dhiraagu - New Service - TGR1A-802 / Service #: BB20213001 / Order ID: 0125858440"
        - "Ooredoo - Relocation - H09-15-07 / 152402"
        - "ROL - Fault - ROL250141"
        """
        result = {"portal": None, "ticket_id": None, "address": None}

        title_lower = title.lower()

        # Detect portal
        if "dhiraagu" in title_lower:
            result["portal"] = "dhiraagu"
            # Extract Order ID or Service #
            order_match = re.search(r"Order ID:\s*(\d+)", title, re.IGNORECASE)
            if order_match:
                result["ticket_id"] = order_match.group(1)
            else:
                service_match = re.search(r"Service #:\s*(\w+)", title, re.IGNORECASE)
                if service_match:
                    result["ticket_id"] = service_match.group(1)

        elif "ooredoo" in title_lower:
            result["portal"] = "ooredoo"
            # Extract ticket number (usually at the end or after /)
            ooredoo_match = re.search(r"/\s*(\d{5,})", title)
            if ooredoo_match:
                result["ticket_id"] = ooredoo_match.group(1)

        elif "rol" in title_lower:
            result["portal"] = "rol"
            # Extract ROL ticket ID (format: ROL + digits)
            rol_match = re.search(r"(ROL\d+)", title, re.IGNORECASE)
            if rol_match:
                result["ticket_id"] = rol_match.group(1)

        elif "medianet" in title_lower:
            result["portal"] = "medianet"
            # Extract Medianet ticket ID
            medianet_match = re.search(r"(SR-\d+|#\s*\d+)", title, re.IGNORECASE)
            if medianet_match:
                result["ticket_id"] = medianet_match.group(1).replace("#", "").strip()

        # Try to extract address (usually between - and / or after specific patterns)
        addr_match = re.search(r"-\s+([A-Z0-9]+-[A-Z0-9]+(?:-\d+)?)\s*[/|]", title)
        if addr_match:
            result["address"] = addr_match.group(1)

        return result

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

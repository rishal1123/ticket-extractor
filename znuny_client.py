"""
Znuny client using Playwright for web-based ticket search.
Searches tickets by subject/title using *ticketid* pattern.
Fetches ticket details including creator, creation time, and article history.
"""

import os
import time
import re
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field

import psutil

# Maldives timezone (UTC+5)
MALDIVES_TZ = timezone(timedelta(hours=5))
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright
from playwright.sync_api import Error as PlaywrightError

from config import Config
from utils.logger import get_logger

# Session directory for persistent Znuny browser
ZNUNY_SESSION_DIR = os.path.join(os.path.dirname(__file__), "data", "browser_sessions", "znuny")

logger = get_logger("znuny")

# Cache TTL in seconds (6 minutes)
CACHE_TTL_SECONDS = 360

# Max entries in ticket details cache before eviction
MAX_DETAIL_CACHE_SIZE = 200


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
    queue: str = ""  # Queue assignment from sidebar
    priority: str = ""  # Ticket priority from sidebar
    address: str = ""  # Address from phone ticket or first article
    znuny_url: str = ""  # Direct URL to ticket in Znuny
    articles: list[ZnunyArticle] = field(default_factory=list)
    total_article_count: int = 0  # Total articles on page (before filtering)


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
    address: str = ""
    customer_name: str = ""


def parse_site_visit_article(article: ZnunyArticle, znuny_ticket_id: str) -> SiteVisit | None:
    """
    Parse an OAN Site Visit Arranged article to extract visit details.

    Expected format in article body:
    Site Type:  Fault ( no BB)
    Service Provider: ooredoo
    Time: 1130
    Assigned to: @maah
    """
    # Check if this is a site visit article (Arranged or Preventative Maintenance)
    # Note: "OAN Site Visit Pending" articles are skipped - they are status notifications
    # with empty assigned_to/scheduled_time that duplicate the "Arranged" articles
    subject_lower = article.subject.lower() if article.subject else ""
    is_site_visit = (
        "oan site visit arranged" in subject_lower
        or "preventative maintenance - site visit" in subject_lower
        or "preventative maintenance -" in subject_lower  # fallback for variations
    )
    if not is_site_visit:
        return None

    body = article.body or ""

    # Parse fields from body
    site_type = ""
    service_provider = ""
    scheduled_time = ""
    assigned_to = ""

    # Site Type (handles "Site Type:", "Site Type :", and "Type:" variations)
    site_match = re.search(r"(?:Site\s*)?Type\s*:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if site_match:
        site_type = site_match.group(1).strip()

    # Service Provider (from body, or infer from subject)
    provider_match = re.search(r"Service Provider:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if provider_match:
        service_provider = provider_match.group(1).strip()
    elif not service_provider:
        # Try to infer from subject (e.g., "Ooredoo OAN Site Visit Arranged")
        for provider in ("dhiraagu", "ooredoo", "rol", "medianet"):
            if provider in subject_lower:
                service_provider = provider.capitalize()
                break

    # Time - can be HHMM format or "now" (now = article creation time)
    time_match = re.search(r"Time:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if time_match:
        raw_time = time_match.group(1).strip().lower()
        if raw_time in ("now", "nnow") and article.created_at:
            scheduled_time = article.created_at.strftime("%H:%M")
        elif re.match(r'^\d{4}$', time_match.group(1).strip()):
            # Convert HHMM to HH:MM (e.g., 1130 -> 11:30)
            t = time_match.group(1).strip()
            scheduled_time = f"{t[:2]}:{t[2:]}"
        else:
            scheduled_time = time_match.group(1).strip()

    # Address
    address = ""
    address_match = re.search(r"Address:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if address_match:
        address = address_match.group(1).strip()

    # Customer Name
    customer_name = ""
    name_match = re.search(r"Customer Name:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if name_match:
        customer_name = name_match.group(1).strip()

    # Assigned to - may have multiple staff: "@maah", "@aslan @ayan", "aslan  @ayan"
    assigned_match = re.search(r"Assigned to:\s*(.+?)(?:\n|$)", body, re.IGNORECASE)
    if assigned_match:
        raw = assigned_match.group(1).strip()
        # Split on @ and whitespace to extract individual names, then rejoin with comma
        names = [n.strip() for n in re.split(r'\s*@\s*', raw) if n.strip()]
        assigned_to = ", ".join(names)

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
        visit_date=visit_date,
        address=address,
        customer_name=customer_name
    )


class ZnunyClient:
    """Client for searching tickets in Znuny using Playwright."""

    # Znuny URLs
    BASE_URL = "https://10.241.1.110"
    LOGIN_URL = f"{BASE_URL}/otrs/index.pl"
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

    # Class-level state (persists across instances for session reuse)
    _shared_playwright = None
    _shared_context = None  # Persistent context (acts as both browser + context)
    _shared_page = None
    _shared_logged_in = False
    _shared_last_login_check = 0  # Timestamp of last successful login verification
    # Class-level caches (persist across instances so sync cycles reuse cached data)
    _shared_open_tickets_cache = None
    _shared_cache_timestamp = None
    _shared_details_cache = {}  # {ticket_number: (ZnunyTicketDetails, timestamp)}
    # Thread safety: protects all shared page operations
    _page_lock = threading.RLock()

    def __init__(self):
        self.username = Config.get_znuny_username()
        self.password = Config.get_znuny_password()

    @property
    def page(self):
        """Get shared page instance."""
        return ZnunyClient._shared_page

    @page.setter
    def page(self, value):
        """Set shared page instance."""
        ZnunyClient._shared_page = value

    @property
    def _logged_in(self):
        """Get shared login state."""
        return ZnunyClient._shared_logged_in

    @_logged_in.setter
    def _logged_in(self, value):
        """Set shared login state."""
        ZnunyClient._shared_logged_in = value

    @property
    def _open_tickets_cache(self):
        """Get shared open tickets cache."""
        return ZnunyClient._shared_open_tickets_cache

    @_open_tickets_cache.setter
    def _open_tickets_cache(self, value):
        """Set shared open tickets cache."""
        ZnunyClient._shared_open_tickets_cache = value

    @property
    def _cache_timestamp(self):
        """Get shared cache timestamp."""
        return ZnunyClient._shared_cache_timestamp

    @_cache_timestamp.setter
    def _cache_timestamp(self, value):
        """Set shared cache timestamp."""
        ZnunyClient._shared_cache_timestamp = value

    @property
    def _ticket_details_cache(self):
        """Get shared ticket details cache."""
        return ZnunyClient._shared_details_cache

    @_ticket_details_cache.setter
    def _ticket_details_cache(self, value):
        """Set shared ticket details cache."""
        ZnunyClient._shared_details_cache = value

    # Memory limit for Znuny browser (MB) - reset if exceeded
    MEMORY_LIMIT_MB = 800
    _shared_browser_pid: int | None = None

    def _get_browser_memory_mb(self) -> float:
        """Get Znuny browser memory usage in MB."""
        pid = ZnunyClient._shared_browser_pid
        if not pid:
            return 0.0
        try:
            parent = psutil.Process(pid)
            total = parent.memory_info().rss
            for child in parent.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return total / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0

    def _detect_browser_pid(self):
        """Detect the Chromium PID spawned by Playwright for memory tracking."""
        try:
            driver_pid = ZnunyClient._shared_playwright._impl_obj._connection._transport._proc.pid
            children = psutil.Process(driver_pid).children(recursive=False)
            for child in children:
                if "chromium" in child.name().lower() or "chrome" in child.name().lower():
                    ZnunyClient._shared_browser_pid = child.pid
                    logger.debug(f"Znuny Chromium PID: {child.pid}")
                    return
            # Fallback: use first child
            if children:
                ZnunyClient._shared_browser_pid = children[0].pid
        except Exception:
            pass

    def _setup_browser(self):
        """Setup Playwright browser with persistent context, reusing existing session if available."""
        if self.page:
            # Check if browser is still alive
            try:
                _ = self.page.url
                # Check memory usage
                mem_mb = self._get_browser_memory_mb()
                if mem_mb > 0:
                    logger.info(f"Reusing existing Znuny browser session (memory: {mem_mb:.0f}MB)")
                    if mem_mb > self.MEMORY_LIMIT_MB:
                        logger.warning(f"Znuny browser memory {mem_mb:.0f}MB exceeds {self.MEMORY_LIMIT_MB}MB - resetting")
                        self._close_browser_resources()
                        self._logged_in = False
                        # Fall through to create new browser
                    else:
                        return
                else:
                    logger.info("Reusing existing Znuny browser session")
                    return
            except Exception:
                logger.info("Znuny browser session died, recreating (session persisted)")
                self._close_browser_resources()
                self._logged_in = False

        os.makedirs(ZNUNY_SESSION_DIR, exist_ok=True)
        try:
            # Playwright's greenlet-based sync API leaves an internal asyncio loop
            # marked as "running" in this thread. If a previous Playwright wasn't
            # stopped cleanly (browser crash, failed pw.stop()), the stale running
            # loop blocks any new sync_playwright().start(). Clear it.
            asyncio._set_running_loop(None)
            asyncio.set_event_loop(asyncio.new_event_loop())
            ZnunyClient._shared_playwright = sync_playwright().start()
            ZnunyClient._shared_context = ZnunyClient._shared_playwright.chromium.launch_persistent_context(
                ZNUNY_SESSION_DIR,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True  # CRITICAL: Znuny uses self-signed cert
            )
            # Persistent context may already have a page
            if ZnunyClient._shared_context.pages:
                ZnunyClient._shared_page = ZnunyClient._shared_context.pages[0]
            else:
                ZnunyClient._shared_page = ZnunyClient._shared_context.new_page()
            ZnunyClient._shared_page.set_default_timeout(10000)
            ZnunyClient._shared_page.set_default_navigation_timeout(15000)
            self._detect_browser_pid()
            logger.info(f"Znuny persistent browser started (session dir: {ZNUNY_SESSION_DIR})")
        except Exception as e:
            logger.error(f"Failed to start Znuny browser: {e}")
            self._close_browser_resources()
            raise

    def _close_browser_resources(self):
        """Close Playwright persistent context and driver safely."""
        # Close page first, then context (which closes browser internally)
        page = ZnunyClient._shared_page
        if page:
            try:
                page.close()
            except Exception:
                pass
            ZnunyClient._shared_page = None
        ctx = ZnunyClient._shared_context
        if ctx:
            try:
                ctx.close()  # Saves session data and closes browser
            except Exception:
                pass
            ZnunyClient._shared_context = None
        pw = ZnunyClient._shared_playwright
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
            ZnunyClient._shared_playwright = None
        ZnunyClient._shared_browser_pid = None
        # Playwright's greenlet-based sync API leaves an internal asyncio loop
        # marked as "running" in this thread. If pw.stop() fails or the browser
        # crashes, this stale loop blocks future sync_playwright().start() calls.
        # Clear the running loop marker so a fresh Playwright can start.
        asyncio._set_running_loop(None)
        asyncio.set_event_loop(asyncio.new_event_loop())
        # Clear caches on browser reset to avoid stale data
        ZnunyClient._shared_open_tickets_cache = None
        ZnunyClient._shared_cache_timestamp = None
        ZnunyClient._shared_details_cache = {}

    def _login(self) -> bool:
        """Login to Znuny, reusing existing session if valid."""
        self._setup_browser()

        if self._logged_in:
            # Skip full verification if checked within last 60 seconds
            if time.time() - ZnunyClient._shared_last_login_check < 60:
                return True
            # Full verification: navigate to a queue page and check we're not on login
            try:
                verify_url = f"{self.BASE_URL}/otrs/index.pl?Action=AgentTicketQueue;QueueID=5;View=Small"
                self.page.goto(verify_url, wait_until="domcontentloaded")
                if "Login" not in (self.page.title() or ""):
                    ZnunyClient._shared_last_login_check = time.time()
                    logger.info("Znuny session still active, skipping login")
                    return True
            except Exception:
                pass
            self._logged_in = False
            logger.info("Znuny session expired, re-logging in...")

        try:
            self.page.goto(self.LOGIN_URL, wait_until="domcontentloaded")

            # Check if already on an authenticated page (session still valid from cookies)
            if "Login" not in (self.page.title() or ""):
                self._logged_in = True
                logger.info("Znuny session valid from cookies")
                return True

            # Enter credentials
            user_field = self.page.query_selector(self.LOGIN_USER_SELECTOR)
            if user_field:
                user_field.fill(self.username)

            password_field = self.page.query_selector(self.LOGIN_PASSWORD_SELECTOR)
            if password_field:
                password_field.fill(self.password)

            # Click login and wait for navigation
            login_btn = self.page.query_selector(self.LOGIN_BUTTON_SELECTOR)
            if login_btn:
                login_btn.click()
                try:
                    self.page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass

            # Verify login — success if we're no longer on the login page
            if "Login" not in (self.page.title() or ""):
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

    # Service View: all ISP/site-visit tickets in one view (replaces per-queue iteration)
    SERVICE_ID = 1  # OAN service - covers all ISP queues + Field Works

    def get_open_tickets(self, force_refresh: bool = False) -> list[dict]:
        """
        Get all open tickets from the Znuny Service View (Small view).
        Uses ServiceID to fetch all ISP tickets in a single view with pagination.
        Returns list of tickets with ticket_number, title, href, state, queue, owner, priority.
        Uses TTL-based cache (5 min) to avoid repeated fetches.
        """
        # Return cached results if valid (TTL-based) - no lock needed for cache read
        if not force_refresh and self._is_cache_valid():
            logger.debug(f"Using cached open tickets (age: {int(time.time() - self._cache_timestamp)}s)")
            return self._open_tickets_cache

        with ZnunyClient._page_lock:
            # Double-check cache inside lock (another thread may have populated it)
            if not force_refresh and self._is_cache_valid():
                return self._open_tickets_cache

            if not self._login():
                return []

            all_tickets = []
            seen = set()
            start_hit = 1
            max_pages = 20  # Safety limit

            for page_num in range(max_pages):
                url = (
                    f"{self.BASE_URL}/otrs/index.pl?Action=AgentTicketService"
                    f";ServiceID={self.SERVICE_ID};Filter=All;View=Small"
                    f";SortBy=Age;OrderBy=Up;StartHit={start_hit}"
                )
                self.page.goto(url, wait_until="domcontentloaded")

                try:
                    self.page.wait_for_selector("tr.MasterAction", timeout=5000)
                except Exception:
                    break  # Empty or no rows on this page

                rows = self.page.query_selector_all("tr.MasterAction")
                if not rows:
                    break

                for row in rows:
                    try:
                        link = row.query_selector("a.MasterActionLink")
                        if not link:
                            continue
                        ticket_number = (link.text_content() or "").strip()
                        if not ticket_number or ticket_number in seen:
                            continue
                        seen.add(ticket_number)

                        href = link.get_attribute("href") or ""
                        cells = row.query_selector_all("td")

                        # Small view columns (0-indexed):
                        # 0=checkbox, 1=Priority, 2=NewArticle, 3=Ticket#, 4=Age,
                        # 5=Sender, 6=Title, 7=State, 8=Lock, 9=Queue, 10=Owner,
                        # 11=CustomerID, 12=Service (service view only)
                        title = ""
                        if len(cells) > 6:
                            title_div = cells[6].query_selector("div[title]")
                            if title_div:
                                title = title_div.get_attribute("title") or (title_div.text_content() or "").strip()

                        state = (cells[7].text_content() or "").strip() if len(cells) > 7 else ""
                        queue = (cells[9].text_content() or "").strip() if len(cells) > 9 else ""
                        owner = (cells[10].text_content() or "").strip() if len(cells) > 10 else ""
                        priority = (cells[1].text_content() or "").strip() if len(cells) > 1 else ""

                        all_tickets.append({
                            "ticket_number": ticket_number,
                            "title": title,
                            "href": href,
                            "state": state,
                            "queue": queue,
                            "owner": owner,
                            "priority": priority,
                        })
                    except Exception:
                        continue

                # Check for next page
                next_start = None
                try:
                    pagination_links = self.page.query_selector_all('a[href*="StartHit="]')
                    for plink in pagination_links:
                        phref = plink.get_attribute('href') or ''
                        match = re.search(r'StartHit=(\d+)', phref)
                        if match:
                            hit_val = int(match.group(1))
                            if hit_val > start_hit:
                                if next_start is None or hit_val < next_start:
                                    next_start = hit_val
                except Exception:
                    pass

                if next_start is None:
                    break  # No more pages

                start_hit = next_start
                logger.debug(f"Service view: page {page_num + 2} (StartHit={start_hit})")

            pages_fetched = page_num + 1 if all_tickets else 0
            logger.info(f"Znuny service view: found {len(all_tickets)} open tickets ({pages_fetched} page{'s' if pages_fetched != 1 else ''})")
            self._open_tickets_cache = all_tickets
            self._cache_timestamp = time.time()
            return all_tickets

    def clear_cache(self):
        """Clear all caches (class-level)."""
        ZnunyClient._shared_open_tickets_cache = None
        ZnunyClient._shared_cache_timestamp = None
        ZnunyClient._shared_details_cache = {}
        ZnunyClient._shared_last_login_check = 0

    def search_by_title(self, search_term: str) -> list[dict]:
        """
        Search for tickets by checking if search_term appears in any open ticket's title.
        Uses cached service view data first, then falls back to Znuny search form.
        Returns list of matching tickets with ticket_number and title.
        """
        # Get all open tickets from service view (uses cache)
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

        if matching:
            logger.info(f"Znuny search for '{search_term}': found {len(matching)} tickets")
            return matching

        # Fallback: use Znuny's search form to find tickets not in service view
        # (e.g. tickets in other services, or with different title format)
        logger.info(f"Znuny search for '{search_term}': not in service view, trying search form")
        fallback = self._search_via_form(search_term)
        if fallback:
            logger.info(f"Znuny search for '{search_term}': found {len(fallback)} via search form")
        else:
            logger.info(f"Znuny search for '{search_term}': not found (service view + search form)")
        return fallback

    def _search_via_form(self, search_term: str) -> list[dict]:
        """
        Search Znuny using the search form as a fallback.
        This finds tickets in ALL queues and states, not just the service view.
        """
        ZnunyClient._page_lock.acquire()
        try:
            if not self._login():
                return []

            # Navigate to search page (loads AJAX dialog)
            self.page.goto(self.SEARCH_URL, wait_until="domcontentloaded")

            # Wait for the search form to appear (may be in a dialog/overlay)
            title_field = None

            # Try direct selector first
            try:
                title_field = self.page.wait_for_selector(
                    self.SEARCH_TITLE_SELECTOR, timeout=5000, state="visible"
                )
            except Exception:
                pass

            # Try alternate selectors if direct didn't work
            if not title_field:
                for selector in ["#Title", "input#Title", "[name='Title']"]:
                    try:
                        title_field = self.page.wait_for_selector(selector, timeout=3000, state="visible")
                        if title_field:
                            logger.info(f"Znuny search: found field with selector '{selector}'")
                            break
                    except Exception:
                        continue

            if not title_field:
                # Log page HTML for debugging
                try:
                    forms = self.page.evaluate("() => Array.from(document.querySelectorAll('input')).map(e => ({name: e.name, id: e.id, type: e.type})).slice(0, 20)")
                    logger.warning(f"Znuny search: title field not found. Inputs on page: {forms}")
                except Exception:
                    logger.warning("Znuny search: title field not found and couldn't inspect page")
                return []

            # Fill search term with wildcards
            title_field.fill(f"*{search_term}*")

            # Submit the search form
            submitted = False
            for btn_selector in [self.SEARCH_SUBMIT_SELECTOR, "button[type='submit']", "#SearchFormSubmit", ".Primary"]:
                try:
                    btn = self.page.query_selector(btn_selector)
                    if btn:
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                logger.warning("Znuny search: couldn't find submit button")
                return []

            # Wait for results page to load
            try:
                self.page.wait_for_selector("tr.MasterAction", timeout=10000)
            except Exception:
                try:
                    self.page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

            page_title = self.page.title() or ""
            logger.info(f"Znuny search results: page title = '{page_title}'")

            # Parse search results
            results = []
            result_rows = self.page.query_selector_all("tr.MasterAction")
            logger.info(f"Znuny search: found {len(result_rows)} result rows")

            for row in result_rows:
                try:
                    link = row.query_selector("a.MasterActionLink")
                    if not link:
                        continue
                    ticket_number = (link.text_content() or "").strip()
                    href = link.get_attribute('href') or ""

                    # Get title
                    title_divs = row.query_selector_all("td div[title]")
                    title = ""
                    if title_divs:
                        title = title_divs[-1].get_attribute('title') or (title_divs[-1].text_content() or "")

                    if ticket_number:
                        results.append({
                            "ticket_number": ticket_number,
                            "title": title,
                            "href": href
                        })
                        logger.info(f"Znuny search result: {ticket_number}: {title[:80]}")
                except Exception:
                    continue

            return results

        except Exception as e:
            logger.error(f"Znuny search form error: {e}")
            return []
        finally:
            ZnunyClient._page_lock.release()

    def search_closed_by_account(self, account: str, ticket_id: str = None) -> list[dict]:
        """Search Znuny tickets by account number using Fulltext search.

        Uses the dashboard 'Any Search' field with Large/Preview view to find
        tickets matching the account number. Extracts ticket number, title,
        creation time, and URL from search results.

        Returns list of matching tickets with ticket_number, title, href, created_at, znuny_url.
        If ticket_id is provided, only returns results whose title contains it.
        """
        ZnunyClient._page_lock.acquire()
        try:
            if not self._login():
                return []

            # Navigate to dashboard
            dashboard_url = f"{self.BASE_URL}/otrs/index.pl?Action=AgentDashboard"
            self.page.goto(dashboard_url, wait_until="domcontentloaded", timeout=15000)
            self.page.wait_for_selector("#Fulltext", timeout=10000)

            # Fill Fulltext search and submit
            fulltext = self.page.query_selector("#Fulltext")
            if not fulltext:
                logger.warning("Account search: Fulltext search field not found on dashboard")
                return []

            fulltext.fill(account)
            fulltext.press("Enter")
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)

            # Check if redirected to a single ticket zoom page
            if "AgentTicketZoom" in self.page.url:
                return self._parse_zoom_page_for_search(ticket_id)

            # Switch to Large/Preview view for Created date
            large_link = self.page.query_selector("a.Large")
            if large_link:
                large_link.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=10000)

            # Parse Large/Preview view items
            results = []
            items = self.page.query_selector_all("li.MasterAction")
            logger.info(f"Account search for '{account}': {len(items)} results")

            if not items:
                return []

            for item in items:
                try:
                    # Ticket number + title from h2 > a.MasterActionLink
                    # Format: "Ticket#2026020228000035 — Title here"
                    link = item.query_selector("a.MasterActionLink")
                    if not link:
                        continue

                    link_text = (link.text_content() or "").strip()
                    title = link.get_attribute("title") or ""
                    href = link.get_attribute("href") or ""

                    ticket_match = re.search(r"Ticket#(\d+)", link_text)
                    ticket_number = ticket_match.group(1) if ticket_match else ""
                    if not ticket_number:
                        continue

                    # Extract Created time from div.Infos: "<label>Created</label>MM/DD/YYYY HH:MM:SS"
                    created_at = None
                    infos = item.query_selector("div.Infos")
                    if infos:
                        infos_text = infos.text_content() or ""
                        created_match = re.search(r"Created\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", infos_text)
                        if created_match:
                            try:
                                created_at = datetime.strptime(created_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                            except ValueError:
                                pass

                    znuny_url = f"{self.BASE_URL}{href}" if href and not href.startswith("http") else href
                    results.append({
                        "ticket_number": ticket_number,
                        "title": title,
                        "href": href,
                        "created_at": created_at,
                        "znuny_url": znuny_url,
                    })
                except Exception:
                    continue

            # Filter by ticket_id if provided
            if ticket_id and results:
                filtered = [r for r in results if ticket_id in r["title"]]
                if filtered:
                    logger.info(f"Account search: matched ticket_id {ticket_id} in {len(filtered)} results")
                    return filtered
                # Fallback: try extract_isp_ticket_id_from_title for each result
                for r in results:
                    extracted = self.extract_isp_ticket_id_from_title(r["title"])
                    if extracted["ticket_id"] == ticket_id:
                        logger.info(f"Account search: matched via title parser: {r['ticket_number']}")
                        return [r]
                logger.info(f"Account search: {len(results)} results for '{account}' but none match {ticket_id}")
                return []

            return results

        except Exception as e:
            logger.error(f"Account search by fulltext failed: {e}")
            return []
        finally:
            ZnunyClient._page_lock.release()

    def _parse_zoom_page_for_search(self, ticket_id: str = None) -> list[dict]:
        """Parse a single ticket from zoom page when search auto-redirects.

        When Fulltext search matches exactly one ticket, Znuny redirects
        directly to the ticket zoom page instead of showing search results.
        """
        try:
            url = self.page.url

            # Ticket number from page title: "2026020228000035 - Zoom - Ticket - Znuny"
            page_title = self.page.title() or ""
            ticket_number_match = re.match(r"(\d+)", page_title)
            ticket_number = ticket_number_match.group(1) if ticket_number_match else ""
            if not ticket_number:
                return []

            # Title from ticket header h1: "Ticket#XXXX — Title here"
            title = ""
            title_el = self.page.query_selector("h1")
            if title_el:
                h1_text = (title_el.text_content() or "").strip()
                # Strip "Ticket#XXXX — " prefix to get just the title
                title_sep = re.split(r"\s*[—–-]\s*", h1_text, maxsplit=1)
                title = title_sep[1] if len(title_sep) > 1 else h1_text

            # Check ticket_id filter
            if ticket_id and ticket_id not in title and ticket_id not in page_title:
                logger.info(f"Account search: zoom redirect to {ticket_number} but doesn't match {ticket_id}")
                return []

            # Parse Created time from sidebar
            created_at = None
            sidebar = self.page.query_selector_all(".SidebarColumn")
            if sidebar:
                sidebar_text = sidebar[0].text_content() or ""
                created_match = re.search(r"Created:\s*\n?(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", sidebar_text)
                if created_match:
                    try:
                        created_at = datetime.strptime(created_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                    except ValueError:
                        pass

            logger.info(f"Account search: zoom redirect to ticket {ticket_number} (created: {created_at})")
            return [{
                "ticket_number": ticket_number,
                "title": title,
                "href": url,
                "created_at": created_at,
                "znuny_url": url,
            }]
        except Exception as e:
            logger.error(f"Failed to parse zoom page for search: {e}")
            return []

    def get_ticket_details(self, ticket_number: str, skip_body_fetch: bool = False,
                           bypass_cache: bool = False) -> ZnunyTicketDetails | None:
        """
        Fetch detailed information about a Znuny ticket.
        Returns ticket details including creation time, creator, and all articles.

        OPTIMIZED with 3 layers:
        1. TTL cache: returns cached details within 5 min (no navigation)
        2. Article count check: navigates but skips parsing if article count unchanged
        3. Full parse: only when new articles detected

        Args:
            ticket_number: The Znuny ticket number
            skip_body_fetch: If True, skip fetching article bodies entirely (fastest mode)
            bypass_cache: If True, skip TTL cache (still uses article-count check)
        """
        # Layer 1: TTL cache (fastest - no navigation needed, no lock needed)
        cached_details = None
        cached_max_article_num = -1
        if ticket_number in self._ticket_details_cache:
            cached_details, cache_time = self._ticket_details_cache[ticket_number]
            cached_max_article_num = max((a.article_number for a in cached_details.articles), default=0)
            if not bypass_cache and time.time() - cache_time < CACHE_TTL_SECONDS:
                logger.debug(f"Using cached details for ticket {ticket_number}")
                return cached_details

        ZnunyClient._page_lock.acquire()
        try:
            if not self._login():
                return None

            return self._fetch_ticket_details(ticket_number, skip_body_fetch, cached_details, cached_max_article_num)
        finally:
            ZnunyClient._page_lock.release()

    MAX_TICKET_PROCESS_SECONDS = 60  # Per-ticket time limit for article processing

    def _fetch_ticket_details(self, ticket_number: str, skip_body_fetch: bool,
                              cached_details, cached_max_article_num: int) -> ZnunyTicketDetails | None:
        """Inner method for get_ticket_details - runs with page lock held."""
        try:
            ticket_process_start = time.time()
            # Find the ticket in cache to get its URL
            all_tickets = self.get_open_tickets()
            ticket_info = next((t for t in all_tickets if t["ticket_number"] == ticket_number), None)

            if not ticket_info or not ticket_info.get("href"):
                logger.warning(f"Ticket {ticket_number} not found in open tickets")
                return None

            # Navigate to ticket detail page (ensure absolute URL)
            href = ticket_info["href"]
            if href.startswith("/"):
                href = self.BASE_URL + href
            self.page.goto(href, wait_until="domcontentloaded")
            try:
                self.page.wait_for_selector(".SidebarColumn", timeout=5000)
            except Exception:
                pass

            # Capture the ticket URL
            znuny_url = self.page.url

            # Parse ticket details from sidebar
            created_at = None
            created_at_str = ""
            created_by = ""
            owner = ""
            state = ""
            queue = ""
            priority = ""

            # Get sidebar content
            sidebar = self.page.query_selector_all(".SidebarColumn")
            if sidebar:
                sidebar_text = (sidebar[0].text_content() or "")

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

                # Extract "Queue:" line
                queue_match = re.search(r"Queue:\s*\n?([^\n]+)", sidebar_text)
                if queue_match:
                    queue = queue_match.group(1).strip()

                # Extract "Priority:" line
                priority_match = re.search(r"Priority:\s*\n?([^\n]+)", sidebar_text)
                if priority_match:
                    priority = priority_match.group(1).strip()

            # Parse articles from article overview table
            articles = []
            article_rows = self.page.query_selector_all(".WidgetSimple table tbody tr")

            # Collect basic article info from table (no clicking needed)
            article_data = []
            for row in article_rows:
                try:
                    cells = row.query_selector_all("td")
                    if len(cells) >= 7:
                        article_num_text = (cells[0].text_content() or "").strip()
                        if not article_num_text.isdigit():
                            continue

                        subject = (cells[5].text_content() or "").strip()
                        via_text = (cells[4].text_content() or "").strip()
                        # Only click articles that need body content:
                        # - Site visit / PM articles (for visit detail parsing)
                        # - Phone articles (for address extraction)
                        # - Internal notes (for staff tracking)
                        # Email articles rarely have actionable content
                        subject_lower = subject.lower()
                        needs_body = (
                            "site visit" in subject_lower
                            or "preventative maintenance" in subject_lower
                            or via_text == "Phone"
                            or via_text == "Internal"
                        )
                        article_data.append({
                            "num": int(article_num_text),
                            "sender": (cells[3].text_content() or "").strip(),
                            "via": via_text,
                            "subject": subject,
                            "created_str": (cells[6].text_content() or "").strip(),
                            "row": row,
                            "needs_body": needs_body
                        })
                except (IndexError, Exception):
                    continue

            # Layer 2: Check max article number (skip parsing if no new articles)
            total_on_page = len(article_data)
            current_max_num = max((d["num"] for d in article_data), default=0)
            if cached_details and current_max_num == cached_max_article_num:
                logger.debug(f"Ticket {ticket_number}: {total_on_page} articles, max #{current_max_num} (unchanged), using cache")
                # Refresh cache timestamp
                self._ticket_details_cache[ticket_number] = (cached_details, time.time())
                return cached_details

            if cached_max_article_num >= 0:
                logger.info(f"Ticket {ticket_number}: new articles (max #{cached_max_article_num} -> #{current_max_num}), re-processing")

            # Filter articles: keep only last article, Phone, and site visit articles
            if article_data:
                article_data = [
                    d for d in article_data
                    if d["num"] == current_max_num
                    or d["via"] == "Phone"
                    or "site visit" in d["subject"].lower()
                    or "preventative maintenance" in d["subject"].lower()
                ]

            # Layer 3: Full article parse (only when new articles detected or first time)
            # Separate articles: need body vs skipped
            raw_needing_body = [d for d in article_data if d["needs_body"] and not skip_body_fetch]
            articles_skipped = [d for d in article_data if not d["needs_body"] or skip_body_fetch]

            # Only first Phone article needs body extraction
            effective_body_articles = []
            phone_seen = False
            for d in raw_needing_body:
                if d["via"] == "Phone":
                    if phone_seen:
                        articles_skipped.append(d)  # Extra phone → treat as skipped
                        continue
                    phone_seen = True
                effective_body_articles.append(d)

            # Add articles that don't need body (fast - no clicking)
            for data in articles_skipped:
                article_created = None
                time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                if time_match:
                    try:
                        article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                    except ValueError:
                        pass

                article_created_by = data["sender"] if data["via"] == "Internal" else ""

                articles.append(ZnunyArticle(
                    article_number=data["num"],
                    sender=data["sender"],
                    via=data["via"],
                    subject=data["subject"],
                    created_at=article_created,
                    created_at_str=data["created_str"],
                    created_by=article_created_by,
                    body=""
                ))

            # Batch extract article bodies in a single page.evaluate() call
            # Eliminates per-article Playwright CDP round-trips (click → wait → extract)
            body_results = {}
            if effective_body_articles:
                body_article_nums = [d["num"] for d in effective_body_articles]
                extract_start = time.time()
                try:
                    self.page.set_default_timeout(30000)  # Allow time for batch
                    body_results = self.page.evaluate("""(articleNums) => {
                        return new Promise(async (resolve) => {
                            const results = {};
                            const table = document.querySelector('.WidgetSimple table tbody');
                            if (!table) { resolve(results); return; }
                            const rows = Array.from(table.querySelectorAll('tr'));

                            // Helper: extract body from current article content area
                            const getBody = () => {
                                const iframes = document.querySelectorAll(
                                    '.ArticleMailContentHTMLWrapper iframe, .ArticleMailContent iframe, iframe[id^="Iframe"]'
                                );
                                for (const iframe of iframes) {
                                    try {
                                        const doc = iframe.contentDocument ||
                                            (iframe.contentWindow && iframe.contentWindow.document);
                                        if (doc && doc.body) {
                                            const text = doc.body.textContent.trim();
                                            if (text) return text;
                                        }
                                    } catch(e) {}
                                }
                                const bodyEl = document.querySelector('.ArticleBody, .MessageBody');
                                if (bodyEl) return bodyEl.textContent.trim();
                                return '';
                            };

                            for (const num of articleNums) {
                                let targetRow = null;
                                for (const row of rows) {
                                    const cells = row.querySelectorAll('td');
                                    if (cells.length >= 7 && cells[0].textContent.trim() === String(num)) {
                                        targetRow = row;
                                        break;
                                    }
                                }
                                if (!targetRow) continue;

                                // Capture previous body to detect content change
                                const prevBody = getBody();

                                // Click to expand article (triggers OTRS AJAX load)
                                targetRow.click();

                                // Poll for body content to change (up to 3s per article)
                                let body = '';
                                let createdBy = '';
                                const start = Date.now();

                                while (Date.now() - start < 3000) {
                                    await new Promise(r => setTimeout(r, 150));
                                    const currentBody = getBody();
                                    if (currentBody && currentBody !== prevBody) {
                                        body = currentBody;
                                        break;
                                    }
                                }
                                // Fallback: use whatever content is there after timeout
                                if (!body) body = getBody();

                                // Extract "by Staff" from article header
                                const headers = document.querySelectorAll('.WidgetSimple h2');
                                for (const h of headers) {
                                    const match = h.textContent.match(/\\bby\\s+([A-Za-z][A-Za-z\\s]+?)\\s*$/m);
                                    if (match) {
                                        createdBy = match[1].trim();
                                        break;
                                    }
                                }

                                results[String(num)] = { body: body || '', createdBy: createdBy || '' };
                            }

                            resolve(results);
                        });
                    }""", body_article_nums) or {}
                except Exception as e:
                    logger.warning(f"Ticket {ticket_number}: batch body extraction failed: {e}")
                    body_results = {}
                finally:
                    self.page.set_default_timeout(10000)

                extract_time = time.time() - extract_start
                extracted_count = sum(1 for r in body_results.values() if r.get("body"))
                logger.info(f"Ticket {ticket_number}: batch extracted {extracted_count}/{len(effective_body_articles)} bodies in {extract_time:.1f}s")

            # Build ZnunyArticle objects for articles with body content
            for data in effective_body_articles:
                article_created = None
                time_match = re.search(r"(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", data["created_str"])
                if time_match:
                    try:
                        article_created = datetime.strptime(time_match.group(1), "%m/%d/%Y %H:%M").replace(tzinfo=MALDIVES_TZ)
                    except ValueError:
                        pass

                result = body_results.get(str(data["num"]), {})
                body = result.get("body", "")
                article_created_by = result.get("createdBy", "")
                if not article_created_by and data["via"] == "Internal":
                    article_created_by = data["sender"]

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

            bodies_extracted = sum(1 for r in body_results.values() if r.get("body"))
            logger.info(f"Fetched details for ticket {ticket_number}: created by {created_by}, {len(articles)} articles (filtered from {total_on_page}, {bodies_extracted} bodies), address={address[:30] if address else 'none'}")

            details = ZnunyTicketDetails(
                ticket_number=ticket_number,
                created_at=created_at,
                created_at_str=created_at_str,
                created_by=created_by,
                owner=owner,
                state=state,
                queue=queue,
                priority=priority,
                address=address,
                znuny_url=znuny_url,
                articles=articles,
                total_article_count=total_on_page
            )

            # Cache the details
            self._ticket_details_cache[ticket_number] = (details, time.time())

            # Evict stale cache entries to prevent unbounded memory growth
            if len(self._ticket_details_cache) > MAX_DETAIL_CACHE_SIZE:
                cutoff = time.time() - CACHE_TTL_SECONDS
                stale = [k for k, (_, ts) in self._ticket_details_cache.items() if ts < cutoff]
                for k in stale:
                    del self._ticket_details_cache[k]
                if stale:
                    logger.debug(f"Evicted {len(stale)} stale entries from detail cache")

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
        - "Ooredoo - Fault - H09-15-07 / Ticket #: 153021"
        - "ROL - Fault - ROL250141"
        - "Medianet - New Service - V3-A-1202 / Account #: 4662628672154865/ Ticket #: S34987"
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
            # Extract ticket number from "Ticket ID: XXXXXX", "Ticket #: XXXXXX", or after /
            ooredoo_match = re.search(r"Ticket\s*ID\W*(\d{5,})", title, re.IGNORECASE)
            if not ooredoo_match:
                ooredoo_match = re.search(r"Ticket\s*#[:\s]*(\d{5,})", title, re.IGNORECASE)
            if not ooredoo_match:
                # Fallback: number directly after / (old format)
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
            # Extract Medianet ticket ID (formats: "Ticket #: S34851", "SR-12345", "S34851")
            medianet_match = re.search(r"Ticket\s*#[:\s]*(\S+)", title, re.IGNORECASE)
            if not medianet_match:
                medianet_match = re.search(r"(SR-\d+|S\d{4,})", title, re.IGNORECASE)
            if medianet_match:
                result["ticket_id"] = medianet_match.group(1).strip()

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

        self._close_browser_resources()
        ZnunyClient._shared_logged_in = False
        logger.info("Znuny browser closed (forced)")

    @classmethod
    def force_close(cls):
        """Force close the shared browser session."""
        page = cls._shared_page
        if page:
            try:
                page.close()
            except Exception:
                pass
            cls._shared_page = None
        ctx = cls._shared_context
        if ctx:
            try:
                ctx.close()  # Saves session data and closes browser
            except Exception:
                pass
            cls._shared_context = None
        pw = cls._shared_playwright
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
            cls._shared_playwright = None
        cls._shared_logged_in = False
        cls._shared_last_login_check = 0
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

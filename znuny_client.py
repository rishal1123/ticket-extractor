"""
Znuny client using the Generic Interface REST API (GenericTicketConnectorREST).

Replaces the previous Playwright-based web-scraping implementation. All ticket
search and detail fetching now goes through Znuny's REST web service:

    POST {host}/otrs/nph-genericinterface.pl/Webservice/GenericTicketConnectorREST/TicketSearch
    GET  {host}/otrs/nph-genericinterface.pl/Webservice/GenericTicketConnectorREST/Ticket/:TicketID

Authentication: the agent UserLogin + Password (ZNUNY_USERNAME / ZNUNY_PASSWORD)
are sent with every request. The Znuny host uses a self-signed certificate, so
TLS verification is disabled.

This module keeps the same public surface as the old Playwright client so it is a
drop-in replacement for ZnunyService and the scheduler:
    - check_ticket_sync(portal_ticket_id) -> (bool, znuny_number | None)
    - search_by_title(term) -> list[dict]
    - get_open_tickets(force_refresh=False) -> list[dict]
    - get_ticket_details(ticket_number, skip_body_fetch=False, bypass_cache=False)
    - search_closed_by_account(account, ticket_id=None) -> list[dict]
    - extract_isp_ticket_id_from_title(title) -> dict
    - get_all_ticket_details() / get_site_visit_tickets()
    - clear_cache() / close() / force_close()

Creator names: the REST API returns the ticket/article creator as a numeric user
id (CreateBy), not a display name. Agent-authored articles, however, expose the
staff member's display name in the From field. We harvest a {user_id: name} map
from agent articles (persisted at class level across the whole sync) and use it to
resolve ticket/article creators. Owner login is used as a fallback when an id has
not been seen yet.
"""

import re
import time
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from config import Config
from utils.logger import get_logger

# Maldives timezone (UTC+5). Znuny's system timezone is Indian/Maldives, so the
# REST API returns timestamps already in Maldives time.
MALDIVES_TZ = timezone(timedelta(hours=5))

logger = get_logger("znuny")

# Cache TTL in seconds (6 minutes)
CACHE_TTL_SECONDS = 360

# Max entries in ticket details cache before eviction
MAX_DETAIL_CACHE_SIZE = 200

# Znuny REST web service
WEBSERVICE_PATH = "/otrs/nph-genericinterface.pl/Webservice/GenericTicketConnectorREST"

# The Znuny Service that ISP portal tickets belong to (service view replacement)
ISP_SERVICE_NAME = "OAN"

# Communication channel id -> human label (Znuny defaults)
CHANNEL_VIA = {1: "Email", 2: "Phone", 3: "Internal"}

# How many ticket ids to request per batched TicketGet call (URL length safety)
TICKETGET_BATCH_SIZE = 20

# HTTP request timeout (seconds)
HTTP_TIMEOUT = 30.0


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
        # Split on @ to extract individual names, then rejoin with comma. Some
        # articles number the assignees as a list ("[1] @raidh [2] @ayan"), so
        # strip "[n]" markers and drop tokens that are only a number marker.
        names = []
        for part in re.split(r'\s*@\s*', raw):
            cleaned = re.sub(r'\[\s*\d+\s*\]', ' ', part)   # remove [1], [2], ...
            cleaned = re.sub(r'\s+', ' ', cleaned).strip(' .,;:-')
            if cleaned:
                names.append(cleaned)
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


def _parse_dt(value: str) -> datetime | None:
    """Parse a Znuny REST timestamp ('YYYY-MM-DD HH:MM:SS') as Maldives time."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=MALDIVES_TZ)
        except ValueError:
            continue
    return None


def _parse_from_name(from_str: str) -> str:
    """Extract a display name from an article From field.

    '"Mohamed Risal" <mohamed.risal@hdc.com.mv>' -> 'Mohamed Risal'
    'Aminath Shazra <a@b.mv>'                     -> 'Aminath Shazra'
    'someone@example.com'                          -> 'someone@example.com'
    """
    if not from_str:
        return ""
    from_str = from_str.strip()
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', from_str)
    if m:
        return m.group(1).strip()
    return from_str.strip().strip('"')


class ZnunyClient:
    """Client for searching/fetching Znuny tickets via the Generic Interface REST API."""

    # Base host (scheme://netloc), derived from the configured Znuny URL.
    BASE_URL = "https://10.241.1.110"

    # ---- Class-level state (persists across instances for reuse across sync cycles) ----
    # Shared HTTP client (connection pooling / keep-alive)
    _shared_http: httpx.Client | None = None

    # Legacy Playwright-era attributes kept so the scheduler's browser-nuke code
    # (which clears these) continues to work unchanged. They are unused by the REST
    # client but harmless to read/write.
    _shared_playwright = None
    _shared_context = None
    _shared_page = None
    _shared_browser_pid = None
    _shared_logged_in = False
    _shared_last_login_check = 0

    # Caches (persist across instances so sync cycles reuse cached data)
    _shared_open_tickets_cache = None
    _shared_cache_timestamp = None
    _shared_details_cache = {}  # {ticket_number: (ZnunyTicketDetails, timestamp)}

    # REST-specific caches
    _number_to_id_cache: dict[str, str] = {}  # {ticket_number: ticket_id}
    _user_names: dict[str, str] = {}  # {user_id (str): display name} harvested from agent articles

    # Thread safety: protects shared cache mutations
    _page_lock = threading.RLock()

    # Memory limit kept for interface compatibility (no browser -> always 0)
    MEMORY_LIMIT_MB = 800

    def __init__(self):
        self.username = Config.get_znuny_username()
        self.password = Config.get_znuny_password()
        configured = Config.get_znuny_url() or ZnunyClient.BASE_URL
        parsed = urlparse(configured)
        if parsed.scheme and parsed.netloc:
            ZnunyClient.BASE_URL = f"{parsed.scheme}://{parsed.netloc}"
        self.base_url = ZnunyClient.BASE_URL
        self.ws_url = f"{self.base_url}{WEBSERVICE_PATH}"

    # ------------------------------------------------------------------ #
    # Cache accessors (kept from the old interface)
    # ------------------------------------------------------------------ #
    @property
    def _open_tickets_cache(self):
        return ZnunyClient._shared_open_tickets_cache

    @_open_tickets_cache.setter
    def _open_tickets_cache(self, value):
        ZnunyClient._shared_open_tickets_cache = value

    @property
    def _cache_timestamp(self):
        return ZnunyClient._shared_cache_timestamp

    @_cache_timestamp.setter
    def _cache_timestamp(self, value):
        ZnunyClient._shared_cache_timestamp = value

    @property
    def _ticket_details_cache(self):
        return ZnunyClient._shared_details_cache

    @_ticket_details_cache.setter
    def _ticket_details_cache(self, value):
        ZnunyClient._shared_details_cache = value

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def _http(self) -> httpx.Client:
        """Lazily create a shared httpx client (self-signed cert -> verify=False)."""
        if ZnunyClient._shared_http is None:
            ZnunyClient._shared_http = httpx.Client(
                verify=False,
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
            )
        return ZnunyClient._shared_http

    def _auth(self) -> dict:
        return {"UserLogin": self.username, "Password": self.password}

    def _ticket_search(self, **criteria) -> list[str]:
        """POST /TicketSearch. Returns a list of TicketID strings (newest first)."""
        payload = {**self._auth(), **criteria}
        try:
            resp = self._http().post(f"{self.ws_url}/TicketSearch", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"TicketSearch failed ({criteria}): {e}")
            return []
        if isinstance(data, dict) and data.get("Error"):
            logger.error(f"TicketSearch error: {data['Error']}")
            return []
        ids = data.get("TicketID") if isinstance(data, dict) else None
        if ids is None:
            return []
        if isinstance(ids, (str, int)):
            ids = [ids]
        return [str(i) for i in ids]

    def _customer_field_exists(self, field: str, value: str) -> "bool | None":
        """True if any ticket matches field==value (exact), False if none, None on
        error/unverifiable. Used by the ticket formatter's address/account checks
        (CustomerID holds the address in this Znuny instance)."""
        value = (value or "").strip()
        if not value:
            return None
        try:
            resp = self._http().post(
                f"{self.ws_url}/TicketSearch",
                json={**self._auth(), field: value},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"TicketSearch {field}={value} failed: {e}")
            return None
        if isinstance(data, dict) and data.get("Error"):
            return None
        ids = data.get("TicketID") if isinstance(data, dict) else None
        return bool(ids)

    def verify_address(self, address_id: str) -> "bool | None":
        """True if a ticket's CustomerID equals this address exactly (else False/None)."""
        return self._customer_field_exists("CustomerID", address_id)

    def account_exists(self, account_id: str) -> "bool | None":
        """True if a ticket exists for this account (CustomerUserLogin) — possible relocation."""
        return self._customer_field_exists("CustomerUserLogin", account_id)

    def _ticket_get(self, ticket_ids: list[str], with_articles: bool = False) -> list[dict]:
        """GET /Ticket/:TicketID for one or more ids (batched, comma-separated)."""
        if not ticket_ids:
            return []

        params = {
            **self._auth(),
            "DynamicFields": 0,
        }
        if with_articles:
            params.update({"AllArticles": 1, "Extended": 1})

        results: list[dict] = []
        for i in range(0, len(ticket_ids), TICKETGET_BATCH_SIZE):
            chunk = ticket_ids[i:i + TICKETGET_BATCH_SIZE]
            url = f"{self.ws_url}/Ticket/{','.join(chunk)}"
            try:
                resp = self._http().get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"TicketGet failed for {chunk}: {e}")
                continue
            if isinstance(data, dict) and data.get("Error"):
                logger.error(f"TicketGet error for {chunk}: {data['Error']}")
                continue
            tickets = data.get("Ticket") if isinstance(data, dict) else None
            if isinstance(tickets, dict):
                tickets = [tickets]
            if tickets:
                results.extend(tickets)
        return results

    # ------------------------------------------------------------------ #
    # User name resolution
    # ------------------------------------------------------------------ #
    def _harvest_user_names(self, ticket: dict):
        """Map CreateBy ids -> display names from agent-authored articles."""
        for art in ticket.get("Article") or []:
            if str(art.get("SenderType")) == "agent":
                uid = art.get("CreateBy")
                name = _parse_from_name(art.get("From") or "")
                if uid is not None and name:
                    ZnunyClient._user_names[str(uid)] = name

    def _name_for(self, user_id, fallback: str = "") -> str:
        if user_id is None:
            return fallback
        return ZnunyClient._user_names.get(str(user_id), fallback)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _zoom_url(self, ticket_id: str) -> str:
        return f"{self.base_url}/otrs/index.pl?Action=AgentTicketZoom;TicketID={ticket_id}"

    def _ticket_to_summary(self, t: dict) -> dict:
        """Build the open-ticket summary dict (matches the old service-view shape)."""
        tid = str(t.get("TicketID", ""))
        number = str(t.get("TicketNumber", ""))
        if number and tid:
            ZnunyClient._number_to_id_cache[number] = tid
        return {
            "ticket_number": number,
            "title": t.get("Title") or "",
            "href": self._zoom_url(tid),
            "state": t.get("State") or "",
            "queue": t.get("Queue") or "",
            "owner": t.get("Owner") or "",
            "priority": t.get("Priority") or "",
        }

    def _resolve_ticket_id(self, ticket_number: str) -> str | None:
        """Resolve a Znuny ticket number to its internal TicketID."""
        cached = ZnunyClient._number_to_id_cache.get(ticket_number)
        if cached:
            return cached
        ids = self._ticket_search(TicketNumber=ticket_number)
        if ids:
            ZnunyClient._number_to_id_cache[ticket_number] = ids[0]
            return ids[0]
        return None

    def _is_cache_valid(self) -> bool:
        if self._open_tickets_cache is None or self._cache_timestamp is None:
            return False
        return (time.time() - self._cache_timestamp) < CACHE_TTL_SECONDS

    def _evict_stale_cache(self):
        """Drop expired and overflowing entries from the details cache."""
        cache = ZnunyClient._shared_details_cache
        if not cache:
            return
        now = time.time()
        stale = [k for k, (_, ts) in cache.items() if now - ts > CACHE_TTL_SECONDS]
        for k in stale:
            cache.pop(k, None)
        # Hard cap: drop oldest entries if still over the limit
        if len(cache) > MAX_DETAIL_CACHE_SIZE:
            for k in sorted(cache, key=lambda kk: cache[kk][1])[:len(cache) - MAX_DETAIL_CACHE_SIZE]:
                cache.pop(k, None)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get_open_tickets(self, force_refresh: bool = False) -> list[dict]:
        """Get all open ISP-service tickets from Znuny.

        Returns list of dicts with ticket_number, title, href, state, queue,
        owner, priority. TTL-cached (6 min) to avoid repeated fetches.
        """
        if not force_refresh and self._is_cache_valid():
            logger.debug(f"Using cached open tickets (age: {int(time.time() - self._cache_timestamp)}s)")
            return self._open_tickets_cache

        with ZnunyClient._page_lock:
            if not force_refresh and self._is_cache_valid():
                return self._open_tickets_cache

            ids = self._ticket_search(
                Services=[ISP_SERVICE_NAME],
                StateType="Open",
                Limit=2000,
            )
            if not ids:
                logger.info("Znuny: no open ISP tickets found")
                self._open_tickets_cache = []
                self._cache_timestamp = time.time()
                return []

            tickets = self._ticket_get(ids, with_articles=False)
            summaries = [self._ticket_to_summary(t) for t in tickets]
            logger.info(f"Znuny service: found {len(summaries)} open tickets")
            self._open_tickets_cache = summaries
            self._cache_timestamp = time.time()
            return summaries

    def clear_cache(self):
        """Clear all caches (class-level)."""
        ZnunyClient._shared_open_tickets_cache = None
        ZnunyClient._shared_cache_timestamp = None
        ZnunyClient._shared_details_cache = {}
        ZnunyClient._shared_last_login_check = 0

    def search_by_title(self, search_term: str) -> list[dict]:
        """Find tickets whose title contains search_term.

        Checks the cached open-ticket list first (zero requests for tickets that
        are currently open — the common case during sync), then falls back to a
        server-side TicketSearch by title for anything else (closed tickets or
        other services).

        Returns list of dicts with ticket_number, title, href, state, queue,
        owner, priority.
        """
        search_lower = search_term.lower()

        # 1) Serve from the already-loaded open-ticket cache when possible.
        cached_matches = [
            t for t in self.get_open_tickets()
            if search_lower in t.get("title", "").lower()
        ]
        if cached_matches:
            logger.info(f"Znuny search for '{search_term}': {len(cached_matches)} in open cache")
            return cached_matches

        # 2) Fall back to a server-side title search (any state / other services).
        ids = self._ticket_search(Title=f"*{search_term}*")
        if not ids:
            logger.info(f"Znuny search for '{search_term}': not found")
            return []

        tickets = self._ticket_get(ids, with_articles=False)
        matching = [
            self._ticket_to_summary(t)
            for t in tickets
            if search_lower in (t.get("Title") or "").lower()
        ]
        if matching:
            logger.info(f"Znuny search for '{search_term}': found {len(matching)} via TicketSearch")
        else:
            logger.info(f"Znuny search for '{search_term}': {len(ids)} candidates but no title match")
        return matching

    def search_closed_by_account(self, account: str, ticket_id: str = None) -> list[dict]:
        """Search Znuny tickets (any state) by account number via Fulltext.

        Returns list of dicts with ticket_number, title, href, created_at, znuny_url.
        If ticket_id is provided, only returns results whose title contains it.
        """
        if not account:
            return []

        ids = self._ticket_search(Fulltext=f"*{account}*")
        if not ids:
            logger.info(f"Account search for '{account}': no results")
            return []

        tickets = self._ticket_get(ids, with_articles=False)
        results = []
        for t in tickets:
            tid = str(t.get("TicketID", ""))
            number = str(t.get("TicketNumber", ""))
            if not number:
                continue
            znuny_url = self._zoom_url(tid)
            results.append({
                "ticket_number": number,
                "title": t.get("Title") or "",
                "href": znuny_url,
                "created_at": _parse_dt(t.get("Created") or ""),
                "znuny_url": znuny_url,
            })

        logger.info(f"Account search for '{account}': {len(results)} results")

        # Filter by ticket_id if provided
        if ticket_id and results:
            filtered = [r for r in results if ticket_id in r["title"]]
            if filtered:
                logger.info(f"Account search: matched ticket_id {ticket_id} in {len(filtered)} results")
                return filtered
            # Fallback: try the title parser for each result
            for r in results:
                extracted = self.extract_isp_ticket_id_from_title(r["title"])
                if extracted["ticket_id"] == ticket_id:
                    logger.info(f"Account search: matched via title parser: {r['ticket_number']}")
                    return [r]
            logger.info(f"Account search: {len(results)} results for '{account}' but none match {ticket_id}")
            return []

        return results

    def get_ticket_details(self, ticket_number: str, skip_body_fetch: bool = False,
                           bypass_cache: bool = False) -> ZnunyTicketDetails | None:
        """Fetch detailed information about a Znuny ticket.

        TTL-cached: within CACHE_TTL_SECONDS a cached copy is returned with no
        request, unless bypass_cache is set. prefetch_open_ticket_details() can
        warm this cache for all open tickets in a few batched calls.

        Args:
            ticket_number: The Znuny ticket number
            skip_body_fetch: If True, article bodies are left empty (faster, less data)
            bypass_cache: If True, skip the TTL cache and always re-fetch
        """
        self._evict_stale_cache()

        if not bypass_cache:
            cached = self._ticket_details_cache.get(ticket_number)
            if cached and time.time() - cached[1] < CACHE_TTL_SECONDS:
                logger.debug(f"Using cached details for ticket {ticket_number}")
                return cached[0]

        with ZnunyClient._page_lock:
            ticket_id = self._resolve_ticket_id(ticket_number)
            if not ticket_id:
                logger.warning(f"Ticket {ticket_number} could not be resolved to a TicketID")
                return None
            tickets = self._ticket_get([ticket_id], with_articles=True)
            if not tickets:
                logger.warning(f"Ticket {ticket_number}: TicketGet returned nothing")
                return None
            t = tickets[0]
            self._harvest_user_names(t)
            return self._build_details_from_ticket(ticket_number, ticket_id, t, skip_body_fetch)

    def prefetch_open_ticket_details(self) -> int:
        """Batch-refresh details for open tickets whose cache entry is missing/expired.

        Collapses the per-ticket TicketGet calls the sync makes in Step 2 into a
        few batched TicketGet calls (chunks of TICKETGET_BATCH_SIZE). Tickets that
        still have a valid TTL cache entry are skipped, so total data volume is
        unchanged — only the number of round-trips drops. Returns count fetched.
        """
        self._evict_stale_cache()
        open_tickets = self.get_open_tickets()
        if not open_tickets:
            return 0

        now = time.time()
        # {ticket_id: ticket_number} for open tickets that need a (re)fetch
        to_fetch: dict[str, str] = {}
        for summary in open_tickets:
            number = summary["ticket_number"]
            cached = self._ticket_details_cache.get(number)
            if cached and now - cached[1] < CACHE_TTL_SECONDS:
                continue
            tid = ZnunyClient._number_to_id_cache.get(number)
            if tid:
                to_fetch[tid] = number

        if not to_fetch:
            return 0

        with ZnunyClient._page_lock:
            tickets = self._ticket_get(list(to_fetch.keys()), with_articles=True)
            # Harvest names across the whole batch first, so a ticket created by an
            # agent who only appears in another ticket's article still resolves.
            for t in tickets:
                self._harvest_user_names(t)
            for t in tickets:
                number = str(t.get("TicketNumber", ""))
                if number:
                    self._build_details_from_ticket(number, str(t.get("TicketID", "")), t,
                                                    skip_body_fetch=False)
        logger.info(f"Prefetched details for {len(tickets)} open tickets ({len(to_fetch)} stale, batched)")
        return len(tickets)

    def get_tickets_by_creators(self, user_ids: list, changed_since: str = None) -> list[dict]:
        """Fetch tickets created by the given Znuny user ids, across every
        service and state (not limited to OAN/open like get_open_tickets).

        Used for the staff-scoped "creator sweep" so each tracked agent's full
        ticket set is ingested. When changed_since (a "YYYY-MM-DD HH:MM:SS" string
        in Znuny system time) is given, only tickets changed at/after that time
        are returned — used for cheap incremental runs after the initial backfill.

        Returns a list of dicts:
            {"details": ZnunyTicketDetails, "title": str,
             "state_type": str (e.g. "open"/"closed"), "closed_at": datetime|None}
        closed_at is the ticket's last-changed time when StateType is closed
        (Znuny TicketGet has no dedicated close timestamp), else None.
        """
        uids = [int(u) for u in user_ids if str(u).isdigit()]
        if not uids:
            return []

        criteria = dict(CreatedUserIDs=uids, Limit=20000)
        if changed_since:
            criteria["TicketChangeTimeNewerDate"] = changed_since
        ids = self._ticket_search(**criteria)
        out = self._fetch_and_build(ids)
        logger.info(f"Creator sweep: fetched {len(out)} tickets for {len(uids)} users")
        return out

    def get_tickets_changed_since(self, changed_since: str) -> list[dict]:
        """Fetch ALL Znuny tickets (every service/creator/state) changed at/after
        `changed_since` ("YYYY-MM-DD HH:MM:SS", Znuny system time). Used to keep
        the current day complete for every ticket type, not just OAN. Returns the
        same item dicts as get_tickets_by_creators."""
        if not changed_since:
            return []
        ids = self._ticket_search(TicketChangeTimeNewerDate=changed_since, Limit=20000)
        out = self._fetch_and_build(ids)
        logger.info(f"Changed-since sweep: fetched {len(out)} tickets since {changed_since}")
        return out

    def _fetch_and_build(self, ids: list) -> list[dict]:
        """Batched TicketGet(+articles) for the given TicketIDs -> item dicts
        {details, title, state_type, closed_at}. Harvests creator names per batch."""
        if not ids:
            return []
        out: list[dict] = []
        with ZnunyClient._page_lock:
            for i in range(0, len(ids), TICKETGET_BATCH_SIZE):
                chunk = ids[i:i + TICKETGET_BATCH_SIZE]
                tickets = self._ticket_get(chunk, with_articles=True)
                # Harvest names across the whole batch first so creators resolve.
                for t in tickets:
                    self._harvest_user_names(t)
                for t in tickets:
                    number = str(t.get("TicketNumber", ""))
                    tid = str(t.get("TicketID", ""))
                    if not number:
                        continue
                    details = self._build_details_from_ticket(number, tid, t, skip_body_fetch=False)
                    state_type = str(t.get("StateType", "")).lower()
                    closed_at = _parse_dt(t.get("Changed") or "") if state_type == "closed" else None
                    out.append({
                        "details": details,
                        "title": t.get("Title") or "",
                        "state_type": state_type,
                        "closed_at": closed_at,
                    })
        return out

    def _build_details_from_ticket(self, ticket_number: str, ticket_id: str, t: dict,
                                   skip_body_fetch: bool) -> ZnunyTicketDetails:
        """Build (and cache) a ZnunyTicketDetails from a TicketGet ticket dict.

        Assumes agent display names have already been harvested from `t` (and,
        ideally, from the rest of the same batch) via _harvest_user_names().
        """
        znuny_url = self._zoom_url(ticket_id)
        created_at_str = t.get("Created") or ""
        created_at = _parse_dt(created_at_str)
        owner = t.get("Owner") or ""
        state = t.get("State") or ""
        queue = t.get("Queue") or ""
        priority = t.get("Priority") or ""
        created_by = self._name_for(t.get("CreateBy"), fallback=owner)

        raw_articles = t.get("Article") or []
        total_on_page = len(raw_articles)

        articles: list[ZnunyArticle] = []
        for a in raw_articles:
            try:
                art_num = int(a.get("ArticleNumber", 0))
            except (TypeError, ValueError):
                continue
            from_str = a.get("From") or ""
            sender = _parse_from_name(from_str)
            try:
                via = CHANNEL_VIA.get(int(a.get("CommunicationChannelID")), "")
            except (TypeError, ValueError):
                via = ""
            art_created_str = a.get("CreateTime") or ""
            art_created = _parse_dt(art_created_str)

            # Attribute notes to the staff who wrote them (agent-sender articles only).
            is_agent = str(a.get("SenderType")) == "agent"
            if is_agent:
                art_created_by = sender or self._name_for(a.get("CreateBy"), "")
            else:
                art_created_by = ""

            body = "" if skip_body_fetch else (a.get("Body") or "")

            articles.append(ZnunyArticle(
                article_number=art_num,
                sender=sender,
                via=via,
                subject=a.get("Subject") or "",
                created_at=art_created,
                created_at_str=art_created_str,
                created_by=art_created_by,
                body=body,
            ))

        # Sort newest-first (descending) to match the previous client's ordering.
        articles.sort(key=lambda a: a.article_number, reverse=True)

        # Extract address from the first phone article body
        address = ""
        for article in articles:
            if article.via == "Phone" and article.body:
                for line in article.body.split('\n'):
                    line_lower = line.lower().strip()
                    if line_lower.startswith('address:') or line_lower.startswith('location:'):
                        addr = line.split(':', 1)[1].strip() if ':' in line else ''
                        if addr:
                            address = addr
                            break
                    elif any(ind in line_lower for ind in
                             ['flat', 'floor', 'building', 'street', 'road', 'lane', 'magu', 'hingun']):
                        address = line.strip()
                        break
                if address:
                    break

        logger.info(
            f"Fetched details for ticket {ticket_number}: created by {created_by}, "
            f"{len(articles)} articles, address={address[:30] if address else 'none'}"
        )

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
            total_article_count=total_on_page,
        )

        self._ticket_details_cache[ticket_number] = (details, time.time())
        return details

    def get_all_ticket_details(self) -> list[ZnunyTicketDetails]:
        """Fetch details for all open tickets."""
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
        """Get all open tickets that have 'site visit' in their title."""
        all_tickets = self.get_open_tickets()
        if not all_tickets:
            return []
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
        """Check if a portal ticket exists in Znuny by searching subject."""
        return self.check_ticket_sync(portal_ticket_id)

    def check_ticket_sync(self, portal_ticket_id: str) -> tuple[bool, str | None]:
        """Check if a portal ticket exists in Znuny. Returns (exists, znuny_number | None)."""
        results = self.search_by_title(portal_ticket_id)
        if results:
            return True, results[0].get("ticket_number")
        return False, None

    def _get_browser_memory_mb(self) -> float:
        """No browser in the REST client; kept for interface compatibility."""
        return 0.0

    def close(self, force: bool = False):
        """Close the HTTP session. By default keeps it alive for reuse."""
        if not force:
            logger.debug("Znuny close called but keeping HTTP session alive for reuse")
            return
        ZnunyClient.force_close()

    @classmethod
    def force_close(cls):
        """Close the shared HTTP client."""
        if cls._shared_http is not None:
            try:
                cls._shared_http.close()
            except Exception:
                pass
            cls._shared_http = None
        cls._shared_logged_in = False
        cls._shared_last_login_check = 0
        logger.info("Znuny HTTP session closed")

    def __del__(self):
        # Don't close on garbage collection - keep session alive
        pass


# Synchronous wrapper for backward compatibility
class ZnunyClientSync:
    """Synchronous Znuny client wrapper."""

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
        """Close the client. By default keeps session alive for reuse."""
        if self._client:
            self._client.close(force=force)
            self._client = None
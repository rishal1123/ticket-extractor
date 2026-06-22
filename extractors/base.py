from abc import ABC, abstractmethod
from typing import Optional
import os
import shutil
import threading
import time
import psutil

from playwright.sync_api import Error as PlaywrightError

from config import PortalConfig, Config
from models.ticket import Ticket
from database import Database
from utils.browser import BrowserManager
from utils.flaresolverr import FlareSolverrClient
from utils.logger import get_logger

# Default memory threshold in MB - subclasses can override via MEMORY_LIMIT_MB
DEFAULT_MEMORY_LIMIT_MB = 800
# Directory for persistent browser sessions
SESSIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_sessions")


class BaseExtractor(ABC):
    """Base class for all portal extractors."""

    # Per-portal browser instances (each portal gets its own Chromium)
    _portal_browsers: dict[str, BrowserManager] = {}
    _portal_browsers_lock = threading.Lock()
    # Track consecutive 0-ticket extraction cycles per portal
    _consecutive_zero_counts: dict = {}
    # Login-failure backoff: with persistent browsers a working portal logs in
    # rarely (session reuse). A portal that keeps FAILING login shouldn't be
    # hammered every cycle — after N consecutive failed cycles it's put in a
    # cooldown and skipped until it expires.
    _login_fail_cycles: dict = {}        # portal -> consecutive failed-login cycles
    _login_cooldown_until: dict = {}     # portal -> epoch seconds to skip until
    LOGIN_FAIL_THRESHOLD = 3
    LOGIN_COOLDOWN_MINUTES = 30
    # Whether to fall back to FlareSolverr when a Cloudflare challenge doesn't
    # auto-clear. Off for portals that self-solve via stealth (Dhiraagu) — the
    # injected, IP-bound cookies make things worse there.
    USE_FLARESOLVERR_FALLBACK = True
    # Browser channel ("chrome" = real Chrome) and forced-headed mode. Cloudflare
    # flags HEADLESS browsers; real Chrome run HEADED passes (matches a human's
    # manual browser). Set both for Cloudflare-protected portals (Dhiraagu).
    BROWSER_CHANNEL = None
    FORCE_HEADED = False
    # Memory limit per browser - override in subclass for heavier portals
    MEMORY_LIMIT_MB = DEFAULT_MEMORY_LIMIT_MB

    def __init__(self, config: PortalConfig, db: Database, headless: bool = False):
        self.config = config
        self.db = db
        self.headless = headless
        self.browser: Optional[BrowserManager] = None
        self.logger = get_logger(f"extractor.{config.name}")
        # Ticket IDs already active in the DB at the start of the current run.
        # Extractors use this to skip detail-page navigation for known tickets
        # (only newly-seen tickets get a full extraction; notes are never fetched —
        # ticket updates come from Znuny). Populated in run() before extract_tickets().
        self._known_ticket_ids: set[str] = set()

    @abstractmethod
    def login(self) -> bool:
        """
        Login to the portal.
        Returns True if login successful, False otherwise.
        """
        pass

    @abstractmethod
    def extract_tickets(self) -> list[Ticket]:
        """
        Extract all tickets from the portal.
        Returns list of Ticket objects.
        """
        pass

    @abstractmethod
    def logout(self):
        """Logout from the portal."""
        pass

    def is_logged_in(self) -> bool:
        """
        Check if currently logged in.
        Override in subclass to implement portal-specific check.
        """
        return False

    def is_known_ticket(self, ticket_id: str) -> bool:
        """Return True if this ticket_id was already active in the DB at run start.

        Extractors should skip per-ticket detail-page navigation for known tickets
        and just register their presence (so they are not marked complete). Only
        newly-seen tickets warrant a full detail extraction.
        """
        return ticket_id in self._known_ticket_ids

    def presence_ticket(self, ticket_id: str) -> "Ticket":
        """Build a minimal Ticket that only marks a known ticket as still present.

        Returned for already-known tickets so they count as found (not completed)
        without triggering a detail-page fetch. base.run() routes these to a
        presence-only DB touch, so the empty fields never overwrite stored data.
        """
        return Ticket(portal=self.config.name, ticket_id=ticket_id)

    def detail_url(self, ticket) -> Optional[str]:
        """Detail-page URL used to capture the raw dump for the formatter.

        Defaults to the stored portal_url (set by SPA portals like Medianet).
        URL-pattern portals (Dhiraagu/Ooredoo/ROL) override to build it from the
        ticket id.
        """
        return getattr(ticket, "portal_url", None)

    def capture_raw_dump(self, ticket) -> Optional[str]:
        """Navigate to the ticket's detail page and return its full text, stored
        as raw_dump and later fed to the ticket formatter. Best-effort: returns
        None on any problem. Assumes an active, logged-in browser for this portal.
        """
        url = self.detail_url(ticket)
        if not url or not self.browser or not self.browser.page:
            return None
        try:
            self.navigate_to(url)
            time.sleep(2)
            text = self.browser.page.inner_text("body")
            return text.strip() or None
        except Exception as e:
            self.logger.warning(f"capture_raw_dump failed for {ticket.ticket_id}: {e}")
            return None

    def fetch_completion_notes(self, missing_ticket_ids: set[str]) -> dict[str, str]:
        """Fetch final notes/comments for tickets about to be marked complete.

        Override in portal-specific extractors that support this.
        Default implementation returns empty dict (no notes fetched).

        Args:
            missing_ticket_ids: Set of ticket_ids that disappeared from the portal

        Returns:
            Dict mapping ticket_id -> notes text
        """
        return {}

    def ensure_logged_in(self) -> bool:
        """Ensure we are logged in, re-login if session expired."""
        if self.is_logged_in():
            self.logger.info("Session still active, skipping login")
            self.db.log_login_event(self.config.name, "session_reused")
            return True
        self.logger.info("Session expired or not logged in, logging in...")
        self.db.log_login_event(self.config.name, "login_attempt")
        success = self.login()
        if success:
            self.db.log_login_event(self.config.name, "login_success")
        else:
            reason = self._diagnose_login_failure()
            self.db.log_login_event(self.config.name, "login_failed", success=False)
            self.db.log_system("error", f"extractor.{self.config.name}", f"Login failed: {reason}")
            self.logger.error(f"Login failed: {reason}")
        return success

    def _diagnose_login_failure(self) -> str:
        """Best-effort reason for why login() returned False, so failures are
        actionable instead of a bare 'Login failed'."""
        try:
            if not self.browser or not self.browser.page:
                return "no browser/page available"
            if self.browser.is_cloudflare_challenge():
                fs = "configured" if Config.get_flaresolverr_url() else "NOT configured"
                return f"Cloudflare challenge blocking login (FlareSolverr {fs})"
            url = (self.browser.page.url or "").lower()
            if "login" in url or "signin" in url or "/account/" in url:
                return f"still on the login page after submit — likely wrong credentials or changed form ({url})"
            return f"login() returned False (current url: {url or 'unknown'})"
        except Exception as e:
            return f"could not diagnose ({e})"

    def _get_browser_memory_mb(self, browser: BrowserManager) -> float:
        """Get total memory usage (MB) for the browser's Chromium process tree."""
        if not browser:
            return 0.0
        pid = browser.get_browser_pid()
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
        except Exception:
            return 0.0

    def _check_memory_and_reset(self, portal: str, browser: BrowserManager) -> Optional[BrowserManager]:
        """Check browser memory usage and reset if over limit. Returns None if reset needed."""
        mem_mb = self._get_browser_memory_mb(browser)
        if mem_mb <= 0:
            return browser
        self.logger.info(f"[{portal}] Browser memory: {mem_mb:.0f} MB (limit: {self.MEMORY_LIMIT_MB} MB)")
        if mem_mb > self.MEMORY_LIMIT_MB:
            self.logger.warning(f"[{portal}] Memory {mem_mb:.0f} MB exceeds {self.MEMORY_LIMIT_MB} MB - resetting browser")
            self.db.log_system(
                "warning", f"extractor.{portal}",
                f"Browser memory {mem_mb:.0f} MB exceeds {self.MEMORY_LIMIT_MB} MB limit - resetting"
            )
            try:
                browser.stop()
            except Exception:
                pass
            with BaseExtractor._portal_browsers_lock:
                BaseExtractor._portal_browsers.pop(portal, None)
            return None
        return browser

    def _get_session_dir(self) -> str:
        """Get persistent session directory for this portal."""
        session_dir = os.path.join(SESSIONS_DIR, self.config.name.lower())
        os.makedirs(session_dir, exist_ok=True)
        return session_dir

    def _clear_session_dir(self):
        """Delete persistent session directory to force a fresh browser next cycle."""
        session_dir = os.path.join(SESSIONS_DIR, self.config.name.lower())
        if os.path.exists(session_dir):
            try:
                shutil.rmtree(session_dir)
                self.logger.info(f"Cleared browser session directory: {session_dir}")
            except Exception as e:
                self.logger.warning(f"Failed to clear session directory: {e}")

    def browser_user_agent(self) -> Optional[str]:
        """User-Agent the portal's browser should use. Default None (Chromium's
        own UA). Cloudflare-protected portals override to match FlareSolverr's UA,
        so injected FlareSolverr cookies are accepted (clearance is UA-bound)."""
        return None

    def _get_or_create_browser(self) -> BrowserManager:
        """Get or create a dedicated browser for this portal.

        Uses Playwright persistent context so login sessions survive browser resets.
        """
        portal = self.config.name
        with BaseExtractor._portal_browsers_lock:
            existing = BaseExtractor._portal_browsers.get(portal)

            if existing is not None:
                if existing.is_alive():
                    # Check memory before reusing
                    checked = self._check_memory_and_reset(portal, existing)
                    if checked is not None:
                        self.logger.info(f"[{portal}] Reusing dedicated browser")
                        return checked
                    # Memory too high - session data persists on disk, safe to recreate
                    self.logger.info(f"[{portal}] Memory limit exceeded, recreating (session persisted)")
                else:
                    self.logger.info(f"[{portal}] Browser died, recreating (session persisted)")
                    self.db.log_system("warning", f"extractor.{portal}", "Browser died, recreating with persisted session")
                    try:
                        existing.stop()
                    except Exception:
                        pass
                    BaseExtractor._portal_browsers.pop(portal, None)

        effective_headless = False if self.FORCE_HEADED else self.headless
        browser = BrowserManager(
            headless=effective_headless,
            user_agent=self.browser_user_agent(),
            channel=self.BROWSER_CHANNEL,
        )
        session_dir = self._get_session_dir()
        browser.start(user_data_dir=session_dir)
        with BaseExtractor._portal_browsers_lock:
            BaseExtractor._portal_browsers[portal] = browser
        self.logger.info(f"[{portal}] Created persistent browser (session dir: {session_dir})")
        return browser

    def run(self, max_retries: int = 3, keep_session: bool = True) -> dict:
        """
        Run the extraction process.
        Returns extraction results.
        """
        result = {
            "portal": self.config.name,
            "status": "failed",
            "tickets_found": 0,
            "tickets_new": 0,
            "tickets_updated": 0,
            "tickets_completed": 0,
            "error": None
        }

        portal = self.config.name

        # Login-failure cooldown: skip this portal while it's backing off so we
        # don't re-attempt a broken login every cycle (login stays rare).
        cooldown_until = BaseExtractor._login_cooldown_until.get(portal, 0)
        if time.time() < cooldown_until:
            mins_left = int((cooldown_until - time.time()) / 60) + 1
            self.logger.warning(f"[{portal}] Login cooldown active ({mins_left}m left) - skipping extraction")
            result["status"] = "skipped"
            result["error"] = f"login cooldown ({mins_left}m left)"
            return result

        got_past_login = False  # set once any attempt clears login
        for attempt in range(max_retries):
            try:
                self.logger.info(f"Starting extraction (attempt {attempt + 1}/{max_retries})")

                # Get current active ticket IDs before extraction (for completion tracking).
                # Also used by extractors to skip detail-page navigation for known tickets.
                existing_ticket_ids = self.db.get_active_ticket_ids(self.config.name)
                self._known_ticket_ids = existing_ticket_ids

                # Get or create browser (reuses existing session if available)
                self.browser = self._get_or_create_browser()

                # Ensure logged in (checks session, re-logins if needed)
                if not self.ensure_logged_in():
                    raise Exception("Login failed")
                got_past_login = True  # any failure after this is extraction, not login

                # Wait for page to stabilize
                time.sleep(2)

                # Extract tickets
                tickets = self.extract_tickets()
                result["tickets_found"] = len(tickets)
                self.logger.info(f"Found {len(tickets)} tickets")

                # Track found ticket IDs
                found_ticket_ids = set()

                # Save tickets to database.
                # New tickets get a full upsert; tickets already known at run start are
                # registered as present only (updated_at touch, no field overwrite) so
                # their data isn't clobbered by deliberately-thin re-extraction — their
                # updates come from Znuny instead.
                present_known_ids = []
                new_captures = []  # (db_id, ticket) for raw-dump capture
                for ticket in tickets:
                    found_ticket_ids.add(ticket.ticket_id)
                    if ticket.ticket_id in self._known_ticket_ids:
                        present_known_ids.append(ticket.ticket_id)
                        continue
                    ticket_id, is_new, is_updated = self.db.upsert_ticket(ticket)
                    if is_new:
                        result["tickets_new"] += 1
                        new_captures.append((ticket_id, ticket))
                    elif is_updated:
                        result["tickets_updated"] += 1

                if present_known_ids:
                    touched = self.db.touch_tickets_seen(self.config.name, present_known_ids)
                    self.logger.info(f"Registered presence of {touched} known tickets (no re-extraction)")

                # Mark missing tickets as complete
                portal_name = self.config.name
                if found_ticket_ids:
                    # Tickets found - reset consecutive zero counter
                    BaseExtractor._consecutive_zero_counts[portal_name] = 0
                    missing_ticket_ids = existing_ticket_ids - found_ticket_ids
                    if missing_ticket_ids:
                        # Fetch final notes before marking complete
                        try:
                            completion_notes = self.fetch_completion_notes(missing_ticket_ids)
                            if completion_notes:
                                self.db.update_ticket_notes_bulk(self.config.name, completion_notes)
                                self.logger.info(f"Updated notes for {len(completion_notes)} tickets before completion")
                        except Exception as e:
                            self.logger.warning(f"Error fetching completion notes (non-critical): {e}")

                        completed_count = self.db.mark_tickets_complete(
                            self.config.name, list(missing_ticket_ids)
                        )
                        result["tickets_completed"] = completed_count
                        self.logger.info(f"Marked {completed_count} tickets as complete (disappeared from portal)")
                elif existing_ticket_ids:
                    # 0 tickets found but active tickets exist
                    # Track consecutive zero-ticket cycles to avoid false completions
                    zero_count = BaseExtractor._consecutive_zero_counts.get(portal_name, 0) + 1
                    BaseExtractor._consecutive_zero_counts[portal_name] = zero_count
                    required_cycles = 3

                    if zero_count >= required_cycles:
                        # 3+ consecutive cycles with 0 tickets - safe to mark complete
                        # Fetch final notes before marking complete
                        try:
                            completion_notes = self.fetch_completion_notes(existing_ticket_ids)
                            if completion_notes:
                                self.db.update_ticket_notes_bulk(self.config.name, completion_notes)
                                self.logger.info(f"Updated notes for {len(completion_notes)} tickets before completion")
                        except Exception as e:
                            self.logger.warning(f"Error fetching completion notes (non-critical): {e}")

                        completed_count = self.db.mark_tickets_complete(
                            self.config.name, list(existing_ticket_ids)
                        )
                        result["tickets_completed"] = completed_count
                        BaseExtractor._consecutive_zero_counts[portal_name] = 0
                        self.logger.info(
                            f"Marked {completed_count} tickets as complete after "
                            f"{zero_count} consecutive zero-ticket cycles"
                        )
                    else:
                        self.logger.warning(
                            f"Extraction returned 0 tickets but {len(existing_ticket_ids)} active - "
                            f"zero-ticket cycle {zero_count}/{required_cycles}, skipping completion"
                        )

                # Capture the raw portal dump (for the ticket formatter) for newly
                # seen tickets, plus a few existing open not-in-Znuny tickets that
                # are still missing it (backfill, capped to keep the cycle light).
                try:
                    targets = list(new_captures)
                    for bt in self.db.get_isp_tickets_needing_dump(self.config.name)[:5]:
                        targets.append((bt.id, bt))
                    done = set()
                    captured = 0
                    for db_id, t in targets:
                        if db_id in done:
                            continue
                        done.add(db_id)
                        dump = self.capture_raw_dump(t)
                        if dump:
                            self.db.set_ticket_raw_dump(db_id, dump)
                            captured += 1
                    if captured:
                        self.logger.info(f"Captured raw dump for {captured} ticket(s)")
                except Exception as e:
                    self.logger.warning(f"Raw dump capture pass error (non-critical): {e}")

                # Don't logout if keeping session active
                if not keep_session:
                    try:
                        self.logout()
                    except Exception as e:
                        self.logger.warning(f"Logout error (non-critical): {e}")

                result["status"] = "success"
                # Log memory usage after successful extraction
                mem_mb = self._get_browser_memory_mb(self.browser)
                if mem_mb > 0:
                    result["memory_mb"] = round(mem_mb, 1)
                self.logger.info(
                    f"Extraction complete: {result['tickets_found']} found, "
                    f"{result['tickets_new']} new, {result['tickets_updated']} updated, "
                    f"{result['tickets_completed']} completed"
                    f"{f', memory: {mem_mb:.0f}MB' if mem_mb > 0 else ''}"
                )
                break

            except Exception as e:
                self.logger.error(f"Extraction failed: {e}")
                result["error"] = str(e)
                self.db.log_system(
                    "error", f"extractor.{self.config.name}",
                    f"Extraction attempt {attempt + 1}/{max_retries} failed: {e}"
                )
                # Kill this portal's browser on error so next attempt starts fresh
                portal = self.config.name
                with BaseExtractor._portal_browsers_lock:
                    existing = BaseExtractor._portal_browsers.pop(portal, None)
                if existing is not None:
                    try:
                        existing.stop()
                    except Exception:
                        pass
                self.browser = None
                if attempt < max_retries - 1:
                    self.logger.info(f"Retrying in 5 seconds...")
                    time.sleep(5)

        # Login-failure backoff bookkeeping. A failed run that never got past
        # login (returned False OR raised in is_logged_in/login) counts toward
        # the streak; extraction-phase failures do not.
        login_failed = result["status"] == "failed" and not got_past_login
        if result["status"] == "success":
            # Recovered — clear any login-failure streak/cooldown.
            BaseExtractor._login_fail_cycles.pop(portal, None)
            BaseExtractor._login_cooldown_until.pop(portal, None)
        elif login_failed:
            cycles = BaseExtractor._login_fail_cycles.get(portal, 0) + 1
            BaseExtractor._login_fail_cycles[portal] = cycles
            if cycles >= self.LOGIN_FAIL_THRESHOLD:
                BaseExtractor._login_cooldown_until[portal] = (
                    time.time() + self.LOGIN_COOLDOWN_MINUTES * 60
                )
                BaseExtractor._login_fail_cycles[portal] = 0
                self.logger.warning(
                    f"[{portal}] Login failed {cycles} cycles in a row - "
                    f"backing off for {self.LOGIN_COOLDOWN_MINUTES} min"
                )
                self.db.log_system(
                    "error", f"extractor.{portal}",
                    f"Login keeps failing ({cycles} cycles); cooling down "
                    f"{self.LOGIN_COOLDOWN_MINUTES} min. Check the last 'Login failed: ...' reason."
                )

        # If all retries failed, clear session data for a fresh start next cycle
        if result["status"] == "failed":
            self.logger.warning(
                f"All {max_retries} attempts failed - clearing browser session for fresh start"
            )
            self._clear_session_dir()
            self.db.log_system(
                "warning", f"extractor.{self.config.name}",
                f"All {max_retries} extraction attempts failed - browser session cleared"
            )

        # Log extraction result
        self.db.log_extraction(
            portal=result["portal"],
            status=result["status"],
            tickets_found=result["tickets_found"],
            tickets_new=result["tickets_new"],
            tickets_updated=result["tickets_updated"],
            tickets_completed=result["tickets_completed"],
            error_message=result["error"]
        )

        return result

    def navigate_to(self, url: str, timeout: int = None, wait_until: str = None):
        """Navigate to a URL. timeout is in milliseconds (default uses page default)."""
        self.logger.debug(f"Navigating to: {url}")
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if wait_until is not None:
            kwargs["wait_until"] = wait_until
        self.browser.page.goto(url, **kwargs)
        # Cloudflare: let the browser solve the JS challenge itself (a realistic
        # UA + stealth evasions clears the "Just a moment" interstitial in a few
        # seconds). Only fall back to FlareSolverr if it doesn't auto-clear AND
        # the portal opts in — FlareSolverr's injected cookies are IP-bound and
        # actively hurt when the browser can self-solve (e.g. Dhiraagu).
        if self.browser.is_cloudflare_challenge():
            if not self._wait_for_cf_autoclear() and self.USE_FLARESOLVERR_FALLBACK:
                self._solve_cloudflare(url, kwargs)

    def _wait_for_cf_autoclear(self, timeout_s: int = 35) -> bool:
        """Poll for the Cloudflare interstitial to clear on its own. Returns True
        if it cleared within timeout_s."""
        for _ in range(timeout_s):
            if not self.browser.is_cloudflare_challenge():
                return True
            self.browser.page.wait_for_timeout(1000)
        cleared = not self.browser.is_cloudflare_challenge()
        if cleared:
            self.logger.info("Cloudflare challenge cleared by the browser itself")
        return cleared

    def _solve_cloudflare(self, url: str, goto_kwargs: dict):
        """Use FlareSolverr to bypass a Cloudflare challenge on the current page."""
        flaresolverr_url = Config.get_flaresolverr_url()
        if not flaresolverr_url:
            self.logger.warning(f"Cloudflare challenge detected on {url} but FLARESOLVERR_URL is not configured")
            return

        self.logger.info(f"Cloudflare challenge detected — asking FlareSolverr to solve {url}")
        self.db.log_system("info", f"extractor.{self.config.name}", f"Cloudflare challenge on {url}, calling FlareSolverr")

        client = FlareSolverrClient(flaresolverr_url)
        result = client.solve(url)
        if not result:
            self.logger.warning("FlareSolverr could not solve the challenge")
            self.db.log_system("warning", f"extractor.{self.config.name}", f"FlareSolverr failed to solve {url}")
            return

        injected = self.browser.inject_cookies(result["cookies"])
        if not injected:
            self.logger.warning("Cookie injection failed after FlareSolverr solve")
            return

        # Retry the navigation with the new cookies in place. Use
        # wait_until="domcontentloaded" (not "load") — on a Cloudflare interstitial
        # this lets the challenge JS run and redirect to the real page reliably
        # (verified: "load" gets stuck on the challenge, "domcontentloaded" clears).
        retry_kwargs = {**goto_kwargs, "wait_until": "domcontentloaded"}
        self.browser.page.goto(url, **retry_kwargs)

        # Cloudflare shows a brief "Just a moment" interstitial that its JS clears
        # (using the injected clearance + matching UA) after a couple seconds and
        # redirects to the real page. Poll for it to clear before concluding —
        # checking immediately catches the transient challenge as a false failure.
        cleared = not self.browser.is_cloudflare_challenge()
        for _ in range(12):
            if cleared:
                break
            self.browser.page.wait_for_timeout(1000)
            cleared = not self.browser.is_cloudflare_challenge()

        if not cleared:
            self.logger.warning("Cloudflare challenge persists after FlareSolverr solve — may need same IP as FlareSolverr")
            self.db.log_system("warning", f"extractor.{self.config.name}", f"Cloudflare challenge persists on {url} after cookie injection")
        else:
            self.logger.info("Cloudflare challenge bypassed successfully")
            self.db.log_system("info", f"extractor.{self.config.name}", f"Cloudflare challenge bypassed on {url}")

    def wait_and_click(self, selector: str, timeout: int = 10) -> bool:
        """Wait for element and click it."""
        return self.browser.safe_click(selector, timeout)

    def wait_and_type(self, selector: str, text: str, timeout: int = 10) -> bool:
        """Wait for element and type text."""
        return self.browser.safe_send_keys(selector, text, timeout)

    def get_element_text(self, selector: str, timeout: int = 10) -> Optional[str]:
        """Get text from element."""
        return self.browser.get_text(selector, timeout)

    def find_elements(self, selector: str) -> list:
        """Find multiple elements."""
        try:
            return self.browser.page.query_selector_all(selector)
        except Exception as e:
            self.logger.warning(f"Error finding elements {selector}: {e}")
            return []

    def take_screenshot(self, name: str):
        """Take screenshot for debugging."""
        if self.browser:
            filename = f"screenshots/{self.config.name}_{name}_{int(time.time())}.png"
            self.browser.take_screenshot(filename)

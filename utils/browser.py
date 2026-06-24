import asyncio
import socket
import threading

import psutil
from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout
import playwright.sync_api._context_manager as _pw_cm

from .logger import get_logger

logger = get_logger("browser")

# ---------------------------------------------------------------------------
# Monkeypatch Playwright's PlaywrightContextManager.__enter__ / .start()
#
# Playwright's sync API checks for a running asyncio event loop and raises
# "It looks like you are using Playwright Sync API inside the asyncio loop"
# if one is detected.  In our setup the worker threads inherit a stale
# "running" loop from either uvicorn (main thread) or a previous Playwright
# greenlet session that wasn't stopped cleanly.
#
# By patching __enter__ we intercept at the *exact* point of failure and
# clear the stale running-loop marker before Playwright's own check runs.
# This is a single global fix that covers every call site (BrowserManager,
# ZnunyClient, extractors) regardless of thread.
# ---------------------------------------------------------------------------
_original_pw_enter = _pw_cm.PlaywrightContextManager.__enter__


def _patched_pw_enter(self):
    import threading as _th
    _tname = _th.current_thread().name
    logger.info(f"[monkeypatch] sync_playwright().start() called from thread '{_tname}'")
    try:
        _loop = asyncio.get_running_loop()
        logger.info(f"[monkeypatch] Found running asyncio loop in thread '{_tname}': {_loop}, running={_loop.is_running()} — clearing it")
    except RuntimeError:
        logger.info(f"[monkeypatch] No running asyncio loop in thread '{_tname}' (good)")
    asyncio._set_running_loop(None)
    asyncio.set_event_loop(asyncio.new_event_loop())
    result = _original_pw_enter(self)
    logger.info(f"[monkeypatch] Playwright started successfully in thread '{_tname}'")
    return result


_pw_cm.PlaywrightContextManager.__enter__ = _patched_pw_enter

# Shared Chromium launch args
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-component-update",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    # Hide the automation flag so Cloudflare's bot checks don't trivially flag us.
    "--disable-blink-features=AutomationControlled",
    # --- Memory reduction (each persistent ISP browser otherwise sits near ~1GB) ---
    # Disable Site Isolation so per-site / per-iframe renderer processes collapse
    # into one — the single biggest memory saver for a one-tab scraper. (Chromium
    # merges multiple --disable-features switches, so this coexists with Playwright's.)
    "--disable-features=IsolateOrigins,site-per-process",
    # One renderer is enough for a single tab; caps process fan-out.
    "--renderer-process-limit=2",
    # Keep on-disk caches tiny — we don't need warm caches between cycles.
    "--disk-cache-size=33554432",
    "--media-cache-size=33554432",
    # No software rasterizer (we already pass --disable-gpu and barely paint).
    "--disable-software-rasterizer",
]

# Extra arg to stop loading/decoding images — a big memory + bandwidth saver for
# portals we only scrape text/DOM from. NOT applied to Cloudflare-protected headed
# portals (Dhiraagu), where the challenge needs to render normally.
DISABLE_IMAGES_ARG = "--blink-settings=imagesEnabled=false"

# Init script applied to every context: mask the most obvious automation tells
# (navigator.webdriver, missing window.chrome / plugins / languages) so the
# browser can clear Cloudflare's JS challenge on its own.
STEALTH_INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {}, app: {} };
try { Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]}); } catch(e){}
try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']}); } catch(e){}
try {
  const op = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (p) => (p && p.name === 'notifications')
    ? Promise.resolve({state: Notification.permission}) : op(p);
} catch(e){}
"""


class BrowserManager:
    # Thread-local storage for shared Playwright instance
    # Each thread gets one Playwright runtime, multiple browsers share it
    _thread_local = threading.local()

    def __init__(self, headless: bool = False, user_agent: str = None, channel: str = None,
                 disable_images: bool = False, cdp_endpoint: str = None, engine: str = "chromium"):
        self.headless = headless
        self.user_agent = user_agent  # override Chromium's default UA (e.g. to match FlareSolverr)
        self.channel = channel        # e.g. "chrome" to use the real Chrome (passes Cloudflare; Chromium gets flagged)
        self.disable_images = disable_images  # skip image loading to cut memory (non-CF portals)
        # Browser engine: "chromium" (default) or "firefox". Firefox (Gecko) is used
        # for Cloudflare-protected Dhiraagu — a different engine that renders reliably
        # headless on Linux where Chrome's renderer crashes, and still passes Cloudflare.
        self.engine = engine
        # "host:port" of a remote Chrome's DevTools endpoint. When set, start()
        # CONNECTS to that already-running headed Chrome over CDP (used for the
        # Dhiraagu browser sidecar) instead of launching a local browser.
        self.cdp_endpoint = cdp_endpoint
        self._connected_cdp = False
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._browser_pid: int | None = None
        self._is_persistent = False

    @classmethod
    def _get_thread_playwright(cls) -> Playwright:
        """Get or create a thread-local Playwright instance."""
        pw = getattr(cls._thread_local, 'playwright', None)
        if pw is None:
            logger.info(f"[BrowserManager] Creating new thread-local Playwright (thread: {threading.current_thread().name})")
            pw = sync_playwright().start()
            cls._thread_local.playwright = pw
            logger.info(f"[BrowserManager] Thread-local Playwright created successfully")
        return pw

    @property
    def driver(self):
        """Backward compatibility - returns the page object."""
        return self.page

    def _snapshot_driver_children(self) -> tuple[int | None, set[int]]:
        """Get driver PID and current child PIDs for Chromium PID detection."""
        try:
            driver_pid = self._playwright._impl_obj._connection._transport._proc.pid
            children = {c.pid for c in psutil.Process(driver_pid).children(recursive=False)}
            return driver_pid, children
        except Exception:
            return None, set()

    def _detect_browser_pid(self, driver_pid: int | None, before_children: set[int]):
        """Find the new Chromium process PID spawned after launch."""
        self._browser_pid = None
        if not driver_pid:
            return
        try:
            after_children = {c.pid for c in psutil.Process(driver_pid).children(recursive=False)}
            new_pids = after_children - before_children
            if new_pids:
                self._browser_pid = new_pids.pop()
                logger.debug(f"Chromium browser PID: {self._browser_pid}")
        except Exception:
            pass

    def start(self, ignore_https_errors: bool = False, user_data_dir: str = None) -> Page:
        """Start browser. If user_data_dir is given, uses persistent context for session reuse."""
        self._playwright = self._get_thread_playwright()

        # Remote browser sidecar: connect to an already-running headed Chrome over
        # CDP instead of launching one locally. The remote Chrome owns the profile,
        # the display and its own lifecycle — we just attach and drive it.
        if self.cdp_endpoint:
            return self._start_cdp(ignore_https_errors)

        driver_pid, before_children = self._snapshot_driver_children()

        is_firefox = self.engine == "firefox"
        browser_type = self._playwright.firefox if is_firefox else self._playwright.chromium

        # Chromium launch args (Firefox doesn't take Chrome flags — it uses prefs).
        launch_args = list(CHROMIUM_ARGS)
        if self.disable_images:
            launch_args.append(DISABLE_IMAGES_ARG)

        if user_data_dir:
            # Persistent context - cookies/localStorage/sessionStorage saved to disk
            logger.info(f"Starting persistent {self.engine} browser (headless={self.headless}, dir={user_data_dir}, images={'off' if self.disable_images else 'on'})")
            self._is_persistent = True
            _ctx_kwargs = dict(
                headless=self.headless,
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=ignore_https_errors,
            )
            if is_firefox:
                # Firefox is configured via prefs, not command-line flags. Mask the
                # obvious automation tell so Cloudflare treats it as a real browser.
                _ctx_kwargs["firefox_user_prefs"] = {
                    "dom.webdriver.enabled": False,
                    "useAutomationExtension": False,
                }
                if self.disable_images:
                    _ctx_kwargs["firefox_user_prefs"]["permissions.default.image"] = 2
            else:
                _ctx_kwargs["args"] = launch_args
                # Drop Playwright's default --enable-automation flag — its automation
                # signals (incl. navigator.webdriver) make Cloudflare Turnstile render
                # as text-only with no clickable checkbox.
                _ctx_kwargs["ignore_default_args"] = ["--enable-automation"]
                if self.channel:
                    _ctx_kwargs["channel"] = self.channel
            if self.user_agent:
                _ctx_kwargs["user_agent"] = self.user_agent
            self._context = browser_type.launch_persistent_context(user_data_dir, **_ctx_kwargs)
            self._browser = None  # No separate Browser object with persistent context
            # Persistent context may already have a page open
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            # Regular context (for ZnunyClient or non-persistent use)
            logger.info(f"Starting {self.engine} browser (headless={self.headless})")
            self._is_persistent = False
            _launch_kwargs = dict(headless=self.headless)
            if not is_firefox:
                _launch_kwargs["args"] = launch_args
            self._browser = browser_type.launch(**_launch_kwargs)
            self._context = self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=ignore_https_errors,
            )
            self.page = self._context.new_page()

        # Apply stealth evasions to all pages (before any navigation runs). Chromium
        # only — the script masks Chrome-specific tells (window.chrome, plugins); on
        # Firefox a fake window.chrome would itself be a tell, and webdriver is already
        # handled via firefox_user_prefs.
        if not is_firefox:
            try:
                self._context.add_init_script(STEALTH_INIT_JS)
            except Exception as e:
                logger.debug(f"Could not add stealth init script: {e}")

        # Detect actual Chromium PID (not the shared Playwright driver PID)
        self._detect_browser_pid(driver_pid, before_children)

        self.page.set_default_timeout(10000)
        logger.info("Browser started successfully")
        return self.page

    def _start_cdp(self, ignore_https_errors: bool) -> Page:
        """Connect to a remote headed Chrome over CDP (the Dhiraagu sidecar).

        The remote Chrome was launched with --user-data-dir, so its default context
        is effectively persistent. We attach to that existing context/page rather
        than creating our own, so the operator's noVNC view and our automation share
        the same window.
        """
        host, _, port = self.cdp_endpoint.partition(":")
        port = port or "9222"
        # Connect by IP, not hostname: Chrome's DevTools HTTP endpoint rejects
        # requests whose Host header isn't an IP literal / localhost, so a service
        # name like "dhiraagu-browser:9222" would 403. Resolving to the IP keeps the
        # Host header an IP and the /json/version handshake succeeds.
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            ip = host
        url = f"http://{ip}:{port}"
        logger.info(f"Connecting to remote Chrome over CDP at {url} (endpoint {self.cdp_endpoint})")

        self._connected_cdp = True
        self._is_persistent = False
        self._browser_pid = None  # remote process; memory is bounded in the sidecar
        self._browser = self._playwright.chromium.connect_over_cdp(url)
        # Reuse the remote browser's existing (persistent) default context + page.
        self._context = (
            self._browser.contexts[0]
            if self._browser.contexts
            else self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=ignore_https_errors,
            )
        )
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()

        try:
            self._context.add_init_script(STEALTH_INIT_JS)
        except Exception as e:
            logger.debug(f"Could not add stealth init script over CDP: {e}")

        self.page.set_default_timeout(10000)
        logger.info("Connected to remote Chrome over CDP")
        return self.page

    def stop(self):
        logger.info("Stopping browser")
        if self._connected_cdp:
            # Remote sidecar Chrome: only DISCONNECT — never close the remote
            # browser or its persistent context (the sidecar owns its lifecycle).
            if self._browser:
                try:
                    self._browser.close()  # for connect_over_cdp this just disconnects
                except Exception as e:
                    logger.warning(f"Error disconnecting from remote Chrome: {e}")
            self.page = None
            self._context = None
            self._browser = None
            self._browser_pid = None
            self._connected_cdp = False
            return
        if self._is_persistent:
            # Persistent context: closing it saves session data and kills browser
            if self._context:
                try:
                    self._context.close()
                except Exception as e:
                    logger.warning(f"Error closing persistent context: {e}")
        else:
            # Regular: close page → context → browser
            for obj, name in [(self.page, "page"), (self._context, "context"),
                              (self._browser, "browser")]:
                if obj:
                    try:
                        obj.close()
                    except Exception as e:
                        logger.warning(f"Error closing {name}: {e}")
        self.page = None
        self._context = None
        self._browser = None
        self._browser_pid = None

    def is_alive(self) -> bool:
        """Check if the browser is still alive."""
        if not self.page:
            return False
        try:
            self.page.url
            return True
        except Exception:
            return False

    def get_browser_pid(self) -> int | None:
        """Get browser process PID for memory tracking."""
        return self._browser_pid

    def wait_for_element(self, selector: str, timeout: int = 10):
        try:
            element = self.page.wait_for_selector(selector, timeout=timeout * 1000, state="attached")
            return element
        except PlaywrightTimeout:
            logger.warning(f"Timeout waiting for element: {selector}")
            return None

    def wait_for_clickable(self, selector: str, timeout: int = 10):
        try:
            element = self.page.wait_for_selector(selector, timeout=timeout * 1000, state="visible")
            return element
        except PlaywrightTimeout:
            logger.warning(f"Timeout waiting for clickable element: {selector}")
            return None

    def safe_click(self, selector: str, timeout: int = 10) -> bool:
        element = self.wait_for_clickable(selector, timeout)
        if element:
            try:
                element.click()
                return True
            except Exception as e:
                logger.warning(f"Failed to click element {selector}: {e}")
        return False

    def safe_send_keys(self, selector: str, keys: str, timeout: int = 10) -> bool:
        element = self.wait_for_element(selector, timeout)
        if element:
            try:
                element.fill(keys)
                return True
            except Exception as e:
                logger.warning(f"Failed to send keys to {selector}: {e}")
        return False

    def get_text(self, selector: str, timeout: int = 10) -> str | None:
        element = self.wait_for_element(selector, timeout)
        if element:
            return element.text_content()
        return None

    def is_cloudflare_challenge(self) -> bool:
        """Return True if the current page is a Cloudflare challenge/CAPTCHA page."""
        if not self.page:
            return False
        try:
            title = self.page.title().lower()
            if "just a moment" in title or "attention required" in title or "ddos-guard" in title:
                return True
            # Fallback: check for known CF DOM markers without fetching full HTML
            for selector in ("div#cf-challenge-running", "#challenge-form", "#cf-hcaptcha-container"):
                if self.page.query_selector(selector):
                    return True
        except Exception:
            pass
        return False

    def inject_cookies(self, cookies: list) -> bool:
        """Add a list of Playwright-format cookies into the current browser context."""
        if not self._context or not cookies:
            return False
        try:
            self._context.add_cookies(cookies)
            return True
        except Exception as e:
            logger.warning(f"Failed to inject cookies: {e}")
            return False

    def take_screenshot(self, filename: str):
        if self.page:
            self.page.screenshot(path=filename)
            logger.info(f"Screenshot saved: {filename}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

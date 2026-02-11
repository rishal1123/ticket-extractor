import threading

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, Playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .logger import get_logger

logger = get_logger("browser")


class BrowserManager:
    # Thread-local storage for shared Playwright instance
    # Each thread gets one Playwright runtime, multiple browsers share it
    _thread_local = threading.local()

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._browser_pid: int | None = None
        self._owns_playwright = False  # Whether this instance created the playwright

    @classmethod
    def _get_thread_playwright(cls) -> Playwright:
        """Get or create a thread-local Playwright instance."""
        pw = getattr(cls._thread_local, 'playwright', None)
        if pw is None:
            pw = sync_playwright().start()
            cls._thread_local.playwright = pw
        return pw

    @property
    def driver(self):
        """Backward compatibility - returns the page object."""
        return self.page

    def start(self, ignore_https_errors: bool = False) -> Page:
        logger.info(f"Starting browser (headless={self.headless})")

        self._playwright = self._get_thread_playwright()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=[
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
            ]
        )

        # Capture browser PID for memory tracking
        try:
            self._browser_pid = self._browser._impl_obj._connection._transport._proc.pid
        except Exception:
            self._browser_pid = None

        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=ignore_https_errors,
        )
        self.page = self._context.new_page()
        self.page.set_default_timeout(10000)
        logger.info("Browser started successfully")
        return self.page

    def stop(self):
        logger.info("Stopping browser")
        # Close page, context, and browser - but NOT playwright (shared per thread)
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

    def take_screenshot(self, filename: str):
        if self.page:
            self.page.screenshot(path=filename)
            logger.info(f"Screenshot saved: {filename}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

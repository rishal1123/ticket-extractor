from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

from .logger import get_logger

logger = get_logger("browser")


class BrowserManager:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self.driver = None

    def start(self) -> webdriver.Chrome:
        logger.info(f"Starting browser (headless={self.headless})")

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")

        # Memory optimization flags
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        options.add_argument("--disable-component-update")
        options.add_argument("--no-first-run")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--js-flags=--max-old-space-size=128")

        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        try:
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service, options=options)
            self.driver.implicitly_wait(10)
            logger.info("Browser started successfully")
            return self.driver
        except WebDriverException as e:
            logger.error(f"Failed to start browser: {e}")
            raise

    def stop(self):
        if self.driver:
            logger.info("Stopping browser")
            try:
                self.driver.quit()
            except Exception as e:
                logger.warning(f"Error stopping browser: {e}")
            finally:
                self.driver = None

    def wait_for_element(self, by: By, value: str, timeout: int = 10):
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return element
        except TimeoutException:
            logger.warning(f"Timeout waiting for element: {by}={value}")
            return None

    def wait_for_clickable(self, by: By, value: str, timeout: int = 10):
        try:
            element = WebDriverWait(self.driver, timeout).until(
                EC.element_to_be_clickable((by, value))
            )
            return element
        except TimeoutException:
            logger.warning(f"Timeout waiting for clickable element: {by}={value}")
            return None

    def safe_click(self, by: By, value: str, timeout: int = 10) -> bool:
        element = self.wait_for_clickable(by, value, timeout)
        if element:
            try:
                element.click()
                return True
            except Exception as e:
                logger.warning(f"Failed to click element {by}={value}: {e}")
        return False

    def safe_send_keys(self, by: By, value: str, keys: str, timeout: int = 10) -> bool:
        element = self.wait_for_element(by, value, timeout)
        if element:
            try:
                element.clear()
                element.send_keys(keys)
                return True
            except Exception as e:
                logger.warning(f"Failed to send keys to {by}={value}: {e}")
        return False

    def get_text(self, by: By, value: str, timeout: int = 10) -> str | None:
        element = self.wait_for_element(by, value, timeout)
        if element:
            return element.text
        return None

    def take_screenshot(self, filename: str):
        if self.driver:
            self.driver.save_screenshot(filename)
            logger.info(f"Screenshot saved: {filename}")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

"""
Test script to explore Medianet ticket detail page structure.
"""

import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from dotenv import load_dotenv
import os

load_dotenv()

def setup_browser():
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(10)
    return driver

def explore_medianet():
    driver = setup_browser()

    try:
        url = os.getenv("MEDIANET_URL", "https://app.crm.com/crm/service-requests-board")
        username = os.getenv("MEDIANET_USERNAME")
        password = os.getenv("MEDIANET_PASSWORD")

        # Login
        print("=== Logging in ===")
        driver.get(url)
        time.sleep(3)

        email_field = driver.find_element(By.CSS_SELECTOR, "input[type='email'], input[name='username']")
        email_field.send_keys(username)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(2)

        password_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        password_field.send_keys(password)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(5)

        # Navigate to Service Requests
        print("=== Navigating to Service Requests Board ===")
        driver.get("https://app.crm.com/crm/service-requests-board")
        time.sleep(5)

        # Get board structure first
        print("\n=== Board Column Structure ===")
        columns = driver.find_elements(By.CSS_SELECTOR, ".board-column")
        for i, col in enumerate(columns):
            try:
                title_el = col.find_elements(By.CSS_SELECTOR, ".card-title, .title")
                title = title_el[0].text if title_el else "Unknown"
                tickets_in_col = col.find_elements(By.CSS_SELECTOR, ".draggable")
                print(f"  Column {i}: '{title.split(chr(10))[0]}' - {len(tickets_in_col)} tickets")
            except:
                pass

        # Click on first ticket to navigate to detail page
        print("\n=== Opening Ticket Detail Page ===")
        tickets = driver.find_elements(By.CSS_SELECTOR, ".draggable")
        if tickets:
            ticket_text = tickets[0].text.split('\n')
            print(f"Clicking ticket: {ticket_text[0] if ticket_text else 'Unknown'}")
            tickets[0].click()
            time.sleep(5)

            print(f"Detail page URL: {driver.current_url}")

            # Get full page text content
            print("\n=== Ticket Detail Page Content ===")
            try:
                # Wait for page to load
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".content-page, main, .page-content"))
                )

                # Get main content area
                content = driver.find_element(By.CSS_SELECTOR, ".content-page, main, body")
                print(f"\nPage text content:\n{content.text[:3000]}")
            except Exception as e:
                print(f"Error getting content: {e}")

            # Look for specific fields
            print("\n=== Looking for Ticket Fields ===")
            field_selectors = [
                ("h1, h2, h3, h4", "Headers"),
                ("[class*='title']", "Title elements"),
                ("[class*='status']", "Status elements"),
                ("[class*='customer']", "Customer elements"),
                ("[class*='contact']", "Contact elements"),
                ("[class*='address']", "Address elements"),
                ("[class*='date']", "Date elements"),
                ("[class*='type']", "Type elements"),
                ("[class*='category']", "Category elements"),
                ("[class*='note']", "Note elements"),
                ("[class*='comment']", "Comment elements"),
                ("[class*='description']", "Description elements"),
                ("table", "Tables"),
                (".card", "Cards"),
                (".tab-pane", "Tab panes"),
                (".nav-tabs a", "Tab links"),
            ]

            for selector, desc in field_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        print(f"\n  Found {len(elements)} '{desc}': {selector}")
                        for i, el in enumerate(elements[:5]):
                            text = el.text[:200] if el.text else ""
                            classes = el.get_attribute('class') or ""
                            if text:
                                print(f"    [{i}] class='{classes[:60]}' text: {text[:150]}")
                except:
                    pass

            # Get page HTML for detailed analysis
            print("\n=== Page HTML Structure (first 15000 chars) ===")
            try:
                main = driver.find_element(By.CSS_SELECTOR, ".content-page, .page-content, main")
                html = main.get_attribute('innerHTML')[:15000]
                print(html)
            except:
                print(driver.page_source[:15000])

        print("\n=== Browser staying open for 15 seconds ===")
        time.sleep(15)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        driver.quit()
        print("\nBrowser closed.")

if __name__ == "__main__":
    explore_medianet()

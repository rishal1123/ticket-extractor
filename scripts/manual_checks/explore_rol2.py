"""
Test script to explore ROL portal grid structure in detail.
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

def explore_rol_grid():
    driver = setup_browser()

    try:
        url = os.getenv("ROL_URL")
        username = os.getenv("ROL_USERNAME")
        password = os.getenv("ROL_PASSWORD")

        # Login
        print("=== Logging in ===")
        driver.get(url)
        time.sleep(2)

        driver.find_element(By.CSS_SELECTOR, "input[name='username']").send_keys(username)
        driver.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(password)
        driver.find_element(By.CSS_SELECTOR, "input[type='submit']").click()
        time.sleep(5)

        print(f"Logged in. URL: {driver.current_url}")

        # Look for the grid container
        print("\n=== Looking for grid container ===")
        grid_selectors = [
            ".gridcontents_ticketmanagegrid_parent",
            "#gridcontents_ticketmanagegrid",
            "[id*='gridcontents']",
            ".gridcontainer",
            ".gridrowcontainer",
        ]

        for selector in grid_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\nFound {len(elements)} elements for '{selector}':")
                    for i, el in enumerate(elements[:3]):
                        text = el.text[:500] if el.text else ""
                        el_id = el.get_attribute('id') or ""
                        classes = el.get_attribute('class') or ""
                        print(f"  [{i}] id='{el_id}' class='{classes[:80]}'")
                        print(f"      text: {text[:300]}")
            except Exception as e:
                print(f"Error: {e}")

        # Look for grid rows
        print("\n=== Looking for grid rows ===")
        row_selectors = [
            ".gridrow",
            "[class*='gridrow']",
            "tr[id*='row']",
            ".swiftgridrow",
        ]

        for selector in row_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\nFound {len(elements)} elements for '{selector}':")
                    for i, el in enumerate(elements[:5]):
                        text = el.text[:300] if el.text else ""
                        el_id = el.get_attribute('id') or ""
                        classes = el.get_attribute('class') or ""
                        onclick = el.get_attribute('onclick') or ""
                        print(f"  [{i}] id='{el_id}' class='{classes[:60]}' onclick='{onclick[:80]}'")
                        print(f"      text: {text[:200]}")
            except Exception as e:
                print(f"Error: {e}")

        # Look for ticket display ID
        print("\n=== Looking for ticket IDs ===")
        id_selectors = [
            "[class*='displayid']",
            "[class*='ticketid']",
            "a[href*='Ticket/View']",
        ]

        for selector in id_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\nFound {len(elements)} elements for '{selector}':")
                    for i, el in enumerate(elements[:5]):
                        text = el.text[:100] if el.text else ""
                        href = el.get_attribute('href') or ""
                        classes = el.get_attribute('class') or ""
                        print(f"  [{i}] text='{text}' href='{href[:80]}' class='{classes[:40]}'")
            except Exception as e:
                print(f"Error: {e}")

        # Get the grid HTML
        print("\n=== Grid HTML structure ===")
        try:
            grid = driver.find_element(By.CSS_SELECTOR, ".gridcontents_ticketmanagegrid_parent")
            html = grid.get_attribute('innerHTML')
            print(html[:5000])
        except Exception as e:
            print(f"Error getting grid HTML: {e}")

        # Navigate directly to a ticket
        print("\n=== Navigating directly to ticket detail ===")
        try:
            ticket_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='Ticket/View']")
            if ticket_links:
                href = ticket_links[0].get_attribute('href')
                print(f"Found ticket link: {href}")
                driver.get(href)
                time.sleep(3)
                print(f"Ticket page URL: {driver.current_url}")
                print(f"Page title: {driver.title}")

                # Look for ticket details
                print("\n=== Ticket detail structure ===")
                detail_selectors = [
                    (".ticketcontentcontainer", "Content container"),
                    (".ticketgeneralcontainer", "General container"),
                    (".ticketproperties", "Properties"),
                    ("#ticketgeneralproperties", "General properties"),
                    (".ticketpostcontainer", "Post container"),
                    (".ticketpost", "Posts"),
                    ("[class*='ticketinfo']", "Ticket info"),
                    (".propertieslist", "Properties list"),
                    (".propertyrow", "Property rows"),
                    ("table.propertytable", "Property table"),
                ]

                for selector, desc in detail_selectors:
                    try:
                        elements = driver.find_elements(By.CSS_SELECTOR, selector)
                        if elements:
                            print(f"\n  Found {len(elements)} '{desc}': {selector}")
                            for i, el in enumerate(elements[:3]):
                                text = el.text[:400] if el.text else ""
                                if text:
                                    print(f"    [{i}] {text[:300]}")
                    except:
                        pass

                # Get ticket page HTML
                print("\n=== Ticket detail HTML (first 10000 chars) ===")
                try:
                    body = driver.find_element(By.TAG_NAME, "body")
                    html = body.get_attribute('innerHTML')[:10000]
                    print(html)
                except Exception as e:
                    print(f"Error: {e}")
        except Exception as e:
            print(f"Error: {e}")

        print("\n=== Keeping browser open for 30 seconds ===")
        time.sleep(30)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        driver.quit()
        print("\nBrowser closed.")

if __name__ == "__main__":
    explore_rol_grid()

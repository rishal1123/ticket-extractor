"""
Test script to explore Znuny/OTRS portal structure.
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
    # Ignore SSL certificate errors (for internal IP)
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(10)
    return driver

def explore_znuny():
    driver = setup_browser()

    try:
        url = os.getenv("ZNUNY_URL")
        username = os.getenv("ZNUNY_USERNAME")
        password = os.getenv("ZNUNY_PASSWORD")

        print(f"=== Navigating to Znuny portal ===")
        print(f"URL: {url}")
        driver.get(url)
        time.sleep(3)

        print(f"\n=== Current URL: {driver.current_url} ===")
        print(f"Page title: {driver.title}")

        # Check if we're on a login page
        print("\n=== Looking for login form ===")
        login_selectors = [
            ("input[name='User']", "User field"),
            ("input[name='Password']", "Password field"),
            ("input[type='text']", "Text inputs"),
            ("input[type='password']", "Password inputs"),
            ("button[type='submit']", "Submit buttons"),
            ("input[type='submit']", "Submit inputs"),
            ("#LoginButton", "Login button"),
            ("form", "Forms"),
        ]

        for selector, desc in login_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                    for i, el in enumerate(elements[:3]):
                        name = el.get_attribute('name') or ''
                        id_attr = el.get_attribute('id') or ''
                        value = el.get_attribute('value') or ''
                        print(f"    [{i}] name='{name}' id='{id_attr}' value='{value[:30]}'")
            except Exception as e:
                print(f"  Error with {selector}: {e}")

        # Try to login
        print("\n=== Attempting login ===")

        # Find username field
        username_field = None
        for selector in ["input[name='User']", "input[name='Username']", "input[name='user']", "input[type='text']"]:
            try:
                username_field = driver.find_element(By.CSS_SELECTOR, selector)
                if username_field:
                    print(f"Found username field: {selector}")
                    break
            except:
                continue

        if username_field:
            username_field.clear()
            username_field.send_keys(username)
            print(f"Entered username: {username}")

        # Find password field
        password_field = None
        for selector in ["input[name='Password']", "input[name='password']", "input[type='password']"]:
            try:
                password_field = driver.find_element(By.CSS_SELECTOR, selector)
                if password_field:
                    print(f"Found password field: {selector}")
                    break
            except:
                continue

        if password_field:
            password_field.clear()
            password_field.send_keys(password)
            print("Entered password")

        # Find and click login button
        login_btn = None
        for selector in ["#LoginButton", "button[type='submit']", "input[type='submit']", "button.Primary"]:
            try:
                login_btn = driver.find_element(By.CSS_SELECTOR, selector)
                if login_btn:
                    print(f"Found login button: {selector}")
                    break
            except:
                continue

        if login_btn:
            login_btn.click()
            print("Clicked login button")
            time.sleep(5)

        print(f"\n=== After login ===")
        print(f"Current URL: {driver.current_url}")
        print(f"Page title: {driver.title}")

        # Check if login was successful
        if "Login" in driver.title:
            print("WARNING: Still on login page - login may have failed")
            # Look for error messages
            error_selectors = [".Error", ".error", ".alert", "[class*='error']", "#LoginError"]
            for selector in error_selectors:
                try:
                    errors = driver.find_elements(By.CSS_SELECTOR, selector)
                    for err in errors:
                        if err.text.strip():
                            print(f"  Error message: {err.text.strip()}")
                except:
                    pass
        else:
            print("Login appears successful!")

        # Explore the dashboard/ticket listing
        print("\n" + "="*60)
        print("=== EXPLORING ZNUNY DASHBOARD ===")
        print("="*60)

        # Look for ticket tables
        print("\n=== Looking for ticket elements ===")
        ticket_selectors = [
            ("table", "Tables"),
            (".MasterActionLink", "Master action links"),
            ("[class*='Ticket']", "Ticket elements"),
            ("[id*='Ticket']", "Ticket ID elements"),
            (".DashboardBox", "Dashboard boxes"),
            (".WidgetSimple", "Widget Simple"),
            ("tr", "Table rows"),
            ("a[href*='AgentTicketZoom']", "Ticket zoom links"),
            ("a[href*='TicketID']", "Ticket ID links"),
        ]

        for selector, desc in ticket_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                    for i, el in enumerate(elements[:5]):
                        text = el.text[:150] if el.text else ""
                        href = el.get_attribute('href') or ""
                        classes = el.get_attribute('class') or ""
                        if text or href:
                            print(f"    [{i}] text: '{text[:80]}' href: {href[:60]} class: {classes[:40]}")
            except Exception as e:
                print(f"  Error with {selector}: {e}")

        # Try to navigate to ticket search
        print("\n=== Looking for search functionality ===")
        search_selectors = [
            ("a[href*='AgentTicketSearch']", "Ticket Search link"),
            ("a[href*='Search']", "Search links"),
            ("#ToolBar", "Toolbar"),
            (".SearchIcon", "Search icon"),
        ]

        for selector, desc in search_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                    for i, el in enumerate(elements[:3]):
                        text = el.text[:100] if el.text else ""
                        href = el.get_attribute('href') or ""
                        print(f"    [{i}] text='{text}' href='{href}'")
            except:
                pass

        # Get page structure
        print("\n=== Page HTML structure (first 5000 chars) ===")
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            html = body.get_attribute('innerHTML')[:5000]
            print(html)
        except Exception as e:
            print(f"Error getting HTML: {e}")

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
    explore_znuny()

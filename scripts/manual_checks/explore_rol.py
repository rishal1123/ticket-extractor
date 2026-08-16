"""
Test script to explore ROL portal (Kayako) structure.
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

def explore_rol():
    driver = setup_browser()

    try:
        url = os.getenv("ROL_URL")
        username = os.getenv("ROL_USERNAME")
        password = os.getenv("ROL_PASSWORD")

        print(f"=== Navigating to ROL portal ===")
        print(f"URL: {url}")
        driver.get(url)
        time.sleep(3)

        print(f"\n=== Current URL: {driver.current_url} ===")
        print(f"Page title: {driver.title}")

        # Check if we're on a login page
        print("\n=== Looking for login form ===")
        login_selectors = [
            ("input[type='text']", "Text inputs"),
            ("input[type='email']", "Email inputs"),
            ("input[name='username']", "Username field"),
            ("input[name='loginname']", "Login name field"),
            ("input[name='staffusername']", "Staff username"),
            ("input[type='password']", "Password inputs"),
            ("input[name='password']", "Password field"),
            ("input[name='staffpassword']", "Staff password"),
            ("button[type='submit']", "Submit buttons"),
            ("input[type='submit']", "Submit inputs"),
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
                        placeholder = el.get_attribute('placeholder') or ''
                        value = el.get_attribute('value') or ''
                        print(f"    [{i}] name='{name}' id='{id_attr}' placeholder='{placeholder}' value='{value[:30]}'")
            except Exception as e:
                print(f"  Error with {selector}: {e}")

        # Try to login
        print("\n=== Attempting login ===")

        # Find username field
        username_field = None
        for selector in ["input[name='staffusername']", "input[name='username']", "input[name='loginname']", "input[type='text']"]:
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
        for selector in ["input[name='staffpassword']", "input[name='password']", "input[type='password']"]:
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
        for selector in ["input[type='submit']", "button[type='submit']", "input[name='loginsubmit']", ".loginbutton"]:
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
        if "login" in driver.current_url.lower():
            print("WARNING: Still on login page - login may have failed")
            # Look for error messages
            error_selectors = [".error", ".alert", ".errormessage", "[class*='error']"]
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

        # Explore the tickets page
        print("\n" + "="*60)
        print("=== EXPLORING TICKETS PAGE ===")
        print("="*60)

        # Look for ticket tables
        print("\n=== Looking for ticket tables ===")
        table_selectors = [
            ("table", "Tables"),
            (".tickettablecontainer", "Ticket table container"),
            ("[class*='ticket']", "Ticket elements"),
            ("tr[id^='ticketid']", "Ticket rows by ID"),
            ("tr.ticketrow", "Ticket row class"),
            (".gridrow", "Grid rows"),
            ("tbody tr", "Table body rows"),
        ]

        for selector, desc in table_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                    for i, el in enumerate(elements[:5]):
                        text = el.text[:200] if el.text else ""
                        classes = el.get_attribute('class') or ""
                        el_id = el.get_attribute('id') or ""
                        if text or classes or el_id:
                            print(f"    [{i}] id='{el_id[:40]}' class='{classes[:60]}' text: {text[:100]}")
            except:
                pass

        # Look for specific ticket data
        print("\n=== Looking for ticket data elements ===")
        data_selectors = [
            ("td", "Table cells"),
            (".ticketid", "Ticket ID"),
            ("[class*='subject']", "Subject"),
            ("[class*='status']", "Status"),
            ("[class*='priority']", "Priority"),
            ("[class*='department']", "Department"),
            ("[class*='owner']", "Owner"),
            ("[class*='date']", "Date"),
            ("a[href*='Ticket']", "Ticket links"),
        ]

        for selector, desc in data_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                    for i, el in enumerate(elements[:5]):
                        text = el.text[:150] if el.text else ""
                        href = el.get_attribute('href') or ""
                        if text or href:
                            print(f"    [{i}] text: '{text[:80]}' href: {href[:60]}")
            except:
                pass

        # Get page HTML structure
        print("\n=== Page HTML structure (first 10000 chars) ===")
        try:
            body = driver.find_element(By.TAG_NAME, "body")
            html = body.get_attribute('innerHTML')[:10000]
            print(html)
        except Exception as e:
            print(f"Error getting HTML: {e}")

        # Click on first ticket if found
        print("\n=== Trying to open a ticket ===")
        try:
            ticket_links = driver.find_elements(By.CSS_SELECTOR, "a[href*='Ticket']")
            if ticket_links:
                print(f"Found {len(ticket_links)} ticket links")
                # Find first actual ticket link (skip navigation)
                for link in ticket_links:
                    href = link.get_attribute('href') or ""
                    if '/Ticket/View/' in href or '/Tickets/Ticket/' in href:
                        print(f"Clicking ticket link: {href}")
                        link.click()
                        time.sleep(3)

                        print(f"\n=== Ticket Detail Page ===")
                        print(f"URL: {driver.current_url}")

                        # Get ticket detail content
                        print("\n=== Ticket detail elements ===")
                        detail_selectors = [
                            (".ticketcontentcontainer", "Ticket content"),
                            ("[class*='ticketinfo']", "Ticket info"),
                            ("[class*='properties']", "Properties"),
                            (".ticketgeneralproperties", "General properties"),
                            (".ticketpostcontainer", "Post container"),
                            ("[class*='reply']", "Replies"),
                        ]

                        for selector, desc in detail_selectors:
                            try:
                                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                                if elements:
                                    print(f"\n  Found {len(elements)} '{desc}': {selector}")
                                    for i, el in enumerate(elements[:3]):
                                        text = el.text[:300] if el.text else ""
                                        if text:
                                            print(f"    [{i}] {text[:200]}")
                            except:
                                pass

                        # Get detail page HTML
                        print("\n=== Detail page HTML (first 8000 chars) ===")
                        try:
                            body = driver.find_element(By.TAG_NAME, "body")
                            html = body.get_attribute('innerHTML')[:8000]
                            print(html)
                        except:
                            pass

                        break
        except Exception as e:
            print(f"Error opening ticket: {e}")

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
    explore_rol()

"""
Test script to search for tickets in Znuny.
"""

import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
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
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--ignore-ssl-errors")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(10)
    return driver

def login(driver):
    """Login to Znuny."""
    url = os.getenv("ZNUNY_URL")
    username = os.getenv("ZNUNY_USERNAME")
    password = os.getenv("ZNUNY_PASSWORD")

    print(f"Logging in to {url}")
    driver.get(url)
    time.sleep(2)

    # Check if already logged in
    if "Dashboard" in driver.title:
        print("Already logged in")
        return True

    # Login
    driver.find_element(By.CSS_SELECTOR, "input[name='User']").send_keys(username)
    driver.find_element(By.CSS_SELECTOR, "input[name='Password']").send_keys(password)
    driver.find_element(By.CSS_SELECTOR, "#LoginButton").click()
    time.sleep(3)

    if "Dashboard" in driver.title:
        print("Login successful")
        return True
    else:
        print("Login failed")
        return False

def search_ticket(driver, search_term):
    """Search for a ticket using the fulltext search."""
    print(f"\n=== Searching for: {search_term} ===")

    # Navigate to ticket search page
    search_url = "https://10.241.1.110/otrs/index.pl?Action=AgentTicketSearch"
    driver.get(search_url)
    time.sleep(2)

    print(f"Search page title: {driver.title}")

    # Look for search form elements
    print("\n=== Search form elements ===")
    form_selectors = [
        ("input[name='Fulltext']", "Fulltext"),
        ("input[name='TicketNumber']", "Ticket Number"),
        ("input[name='Title']", "Title"),
        ("input[name='CustomerID']", "Customer ID"),
        ("input[name='CustomerUserLogin']", "Customer User"),
        ("select[name='StateIDs']", "State"),
        ("select[name='QueueIDs']", "Queue"),
        ("button[type='submit']", "Submit button"),
        ("#SearchFormSubmit", "Search submit"),
    ]

    for selector, desc in form_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                print(f"  Found {len(elements)} '{desc}': {selector}")
        except:
            pass

    # Try searching by title
    print(f"\n=== Searching by Title: {search_term} ===")
    try:
        title_field = driver.find_element(By.CSS_SELECTOR, "input[name='Title']")
        title_field.clear()
        title_field.send_keys(f"*{search_term}*")
        print(f"Entered title search: *{search_term}*")

        # Click search button
        search_btn = driver.find_element(By.CSS_SELECTOR, "#SearchFormSubmit")
        search_btn.click()
        time.sleep(3)

        print(f"\n=== Search results ===")
        print(f"URL after search: {driver.current_url}")

        # Look for results
        results = driver.find_elements(By.CSS_SELECTOR, "a[href*='AgentTicketZoom']")
        print(f"Found {len(results)} ticket links")

        if results:
            for i, result in enumerate(results[:5]):
                text = result.text.strip()
                href = result.get_attribute('href')
                print(f"  [{i}] {text} -> {href[:80]}...")

            # Also look for ticket titles
            titles = driver.find_elements(By.CSS_SELECTOR, "td.Title, .Title a")
            for i, title in enumerate(titles[:5]):
                print(f"  Title [{i}]: {title.text[:100]}")

        return len(results) > 0

    except Exception as e:
        print(f"Error during search: {e}")
        import traceback
        traceback.print_exc()
        return False

def search_via_toolbar(driver, search_term):
    """Search using the toolbar fulltext search."""
    print(f"\n=== Toolbar search for: {search_term} ===")

    # Go to a known page for toolbar search
    driver.get("https://10.241.1.110/otrs/index.pl?Action=AgentTicketQueue;QueueID=5;View=Small")
    time.sleep(2)

    try:
        # Find the fulltext search in toolbar
        fulltext_input = driver.find_element(By.CSS_SELECTOR, "input#Fulltext")
        fulltext_input.clear()
        fulltext_input.send_keys(search_term)
        fulltext_input.send_keys(Keys.RETURN)
        time.sleep(3)

        print(f"After toolbar search URL: {driver.current_url}")

        # Check if redirected to ticket zoom (exact match)
        if "AgentTicketZoom" in driver.current_url:
            print("FOUND: Redirected directly to ticket!")
            ticket_number = driver.find_element(By.CSS_SELECTOR, ".TicketNumber, h1").text
            print(f"Ticket: {ticket_number}")
            return True

        # Otherwise check search results
        results = driver.find_elements(By.CSS_SELECTOR, "a[href*='AgentTicketZoom']")
        print(f"Found {len(results)} results")

        for i, result in enumerate(results[:5]):
            print(f"  [{i}] {result.text}")

        return len(results) > 0

    except Exception as e:
        print(f"Error: {e}")
        return False

def test_znuny_search():
    driver = setup_browser()

    try:
        if not login(driver):
            return

        # Test search with some known portal ticket IDs
        test_ids = [
            "OOR-2001287",  # Ooredoo format
            "DHG-12345",    # Dhiraagu format
            "ROL250141",    # ROL format
            "S34817",       # Medianet format
        ]

        for ticket_id in test_ids:
            found = search_ticket(driver, ticket_id)
            print(f"\n>>> {ticket_id}: {'FOUND' if found else 'NOT FOUND'}")
            time.sleep(2)

            # Go back to search page for next test
            driver.get("https://10.241.1.110/otrs/index.pl?Action=AgentTicketSearch")
            time.sleep(1)

        print("\n=== Keeping browser open for 20 seconds ===")
        time.sleep(20)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        driver.quit()
        print("\nBrowser closed.")

if __name__ == "__main__":
    test_znuny_search()

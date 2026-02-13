"""
LinkedIn authentication module — handles login with Selenium.
"""

import random
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

from config import Config
from utils import get_logger, human_delay

logger = get_logger("auth")

LOGIN_URL = "https://www.linkedin.com/login"

# ── Selectors that ONLY exist for authenticated users ──────────────
# The global nav "Me" dropdown photo/icon is a reliable signal.
_AUTHENTICATED_SELECTORS = [
    ".global-nav__me-photo",                     # profile photo in nav
    ".global-nav__me",                           # "Me" menu container
    "img.global-nav__me-photo",                  # img variant
    ".feed-identity-module",                     # left-rail identity card on /feed
    "[data-control-name='identity_welcome_message']",
    ".scaffold-layout__main",                    # main feed scaffold (only for members)
]

# Pages that confirm we are NOT logged in
_GUEST_URL_FRAGMENTS = [
    "/login", "/signup", "/authwall", "/uas/login",
    "/checkpoint/lg/", "/m/login", "guest",
]


def login(driver: webdriver.Chrome) -> bool:
    """
    Log into LinkedIn. Returns True on success.
    If a Chrome profile with an active session is used, this may
    already be logged in — we detect that and skip.
    """
    logger.info("Checking for existing LinkedIn session …")
    driver.get("https://www.linkedin.com/feed/")

    # Wait for the page to settle — LinkedIn may redirect guests
    human_delay(4, 6)

    # Check if session from Chrome profile is still valid
    if _is_logged_in(driver):
        logger.info("✅ Already logged in (session from Chrome profile).")
        return True

    logger.info("Not logged in. Navigating to login page …")
    driver.get(LOGIN_URL)
    human_delay(2, 3)

    try:
        wait = WebDriverWait(driver, 15)

        # Email
        email_field = wait.until(
            EC.presence_of_element_located((By.ID, "username"))
        )
        email_field.clear()
        _type_like_human(email_field, Config.LINKEDIN_EMAIL)
        human_delay(0.5, 1)

        # Password
        pw_field = driver.find_element(By.ID, "password")
        pw_field.clear()
        _type_like_human(pw_field, Config.LINKEDIN_PASSWORD)
        human_delay(0.5, 1)

        # Submit
        pw_field.send_keys(Keys.RETURN)
        logger.info("Credentials submitted, waiting for response …")
        human_delay(4, 6)

        # Handle possible security checkpoint / CAPTCHA
        current = driver.current_url
        if "checkpoint" in current or "challenge" in current:
            logger.warning(
                "⚠️  Security checkpoint detected! "
                "Please solve it manually in the browser window. "
                "Waiting up to 120 seconds …"
            )
            try:
                WebDriverWait(driver, 120).until(_is_logged_in)
            except TimeoutException:
                logger.error("Timed out waiting for checkpoint resolution.")
                return False

        # Final verification — give the feed page time to load
        if "feed" not in driver.current_url:
            driver.get("https://www.linkedin.com/feed/")
            human_delay(3, 5)

        if _is_logged_in(driver):
            logger.info("✅ Login successful.")
            return True
        else:
            _log_login_failure(driver)
            return False

    except Exception as e:
        logger.error(f"Login error: {e}")
        return False


def _is_logged_in(driver: webdriver.Chrome) -> bool:
    """
    Robustly check if the current page is an authenticated LinkedIn session.
    We look for DOM elements that only exist for logged-in members,
    and explicitly reject known guest/login page URLs.
    """
    try:
        current_url = driver.current_url.lower()

        # Definitely NOT logged in if we're on a guest/login page
        for fragment in _GUEST_URL_FRAGMENTS:
            if fragment in current_url:
                logger.debug(f"Guest URL detected: {current_url}")
                return False

        # Check page meta for guest indicators (member-id="0" = guest)
        try:
            config_meta = driver.find_element(By.CSS_SELECTOR, "meta#config")
            member_id = config_meta.get_attribute("data-member-id")
            if member_id == "0":
                logger.debug("Page meta says member-id=0 (guest).")
                return False
        except Exception:
            pass  # meta tag may not exist on all pages

        # Look for authenticated-only DOM elements
        for selector in _AUTHENTICATED_SELECTORS:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                logger.debug(f"Authenticated element found: {selector}")
                return True

        # Fallback: check that we're on /feed/ AND the page title isn't a guest page
        if "/feed" in current_url:
            title = driver.title.lower()
            # Authenticated feed has titles like "LinkedIn" or "(3) Feed | LinkedIn"
            # Guest feed has titles like "Log In or Sign Up" or "LinkedIn: Log In or Sign Up"
            if "log in" not in title and "sign up" not in title and "join" not in title:
                logger.debug(f"On /feed with authenticated title: {driver.title}")
                return True

        logger.debug(f"No authenticated signals found. URL: {current_url}")
        return False

    except Exception as e:
        logger.debug(f"_is_logged_in check error: {e}")
        return False


def _log_login_failure(driver: webdriver.Chrome):
    """Save diagnostic info when login fails."""
    logger.error("❌ Login failed — could not verify authenticated session.")
    logger.error(f"  Current URL: {driver.current_url}")
    logger.error(f"  Page title:  {driver.title}")
    try:
        from pathlib import Path
        debug_dir = Path(__file__).parent / "data"
        debug_dir.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(debug_dir / "login_failure.png"))
        (debug_dir / "login_failure.html").write_text(
            driver.page_source, encoding="utf-8"
        )
        logger.error("  📸 Screenshot & page source saved to data/login_failure.*")
    except Exception:
        pass


def _type_like_human(element, text: str):
    """Type text character-by-character with slight delays."""
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.04, 0.15))

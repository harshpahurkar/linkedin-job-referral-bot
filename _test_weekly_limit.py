"""
Test: verify weekly invitation limit detection.

Sends a connection request that WILL trigger LinkedIn's weekly limit popup.
Screenshots every step and checks if _check_weekly_limit_message() catches it.
"""

import os
import time
import traceback
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains

from config import Config
from models import Database, Job, Contact
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session
from messenger import (
    _click_connect_button,
    _send_connection_with_note,
    _check_weekly_limit_message,
    _pick_message,
)

logger = get_logger("test_weekly_limit")

SCREENSHOTS_DIR = os.path.join("data", "screenshots", "test_weekly_limit")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# A contact we haven't connected with yet
TEST_CONTACT = Contact(
    contact_id="nishita-sachdev",
    name="Nishita Sachdev",
    first_name="Nishita",
    profile_url="https://www.linkedin.com/in/nishita-sachdev/",
    company="Test Company",
    title="Software Engineer",
)


def screenshot(driver, name):
    ts = datetime.now().strftime("%H%M%S")
    path = os.path.join(SCREENSHOTS_DIR, f"{ts}_{name}.png")
    driver.save_screenshot(path)
    logger.info(f"  📸 {name} → {path}")
    return path


def main():
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    db = Database()
    driver = None

    try:
        logger.info("=" * 60)
        logger.info("🧪 WEEKLY LIMIT DETECTION TEST")
        logger.info("=" * 60)

        driver = create_driver()
        reset_session()

        logger.info("🔐 Logging in …")
        if not login(driver):
            logger.error("❌ Login failed")
            return
        logger.info("✅ Logged in")
        time.sleep(2)

        # Use any job from DB for message generation
        row = db.conn.execute(
            "SELECT * FROM jobs LIMIT 1"
        ).fetchone()
        if not row:
            logger.error("No jobs in DB")
            return

        job = Job(
            job_id=row["job_id"], title=row["title"], company=row["company"],
            location=row["location"], url=row["url"],
            description=row["description"], date_scraped=row["date_scraped"],
        )

        message = _pick_message(TEST_CONTACT, job)
        logger.info(f"📝 Message: {message[:80]}…")

        # ── Step 1: Navigate to profile ──────────────────────────
        logger.info(f"🌐 Navigating to {TEST_CONTACT.profile_url}")
        driver.get(TEST_CONTACT.profile_url)
        time.sleep(3)
        screenshot(driver, "01_profile_loaded")

        # ── Step 2: Try sending connection (will trigger limit) ──
        logger.info("📨 Calling _send_connection_with_note (expecting weekly limit) …")
        result = _send_connection_with_note(driver, TEST_CONTACT, message)
        logger.info(f"  Result: {result}")
        screenshot(driver, "02_after_send_attempt")

        # ── Step 3: Also manually check for the limit message ────
        logger.info("🔍 Manual check: _check_weekly_limit_message() …")
        detected = _check_weekly_limit_message(driver)
        logger.info(f"  Detected: {detected}")

        # Also dump the page text around "limit" or "weekly"
        page_text = driver.execute_script("return document.body.innerText || '';")
        for line in page_text.split("\n"):
            low = line.lower()
            if "limit" in low or "weekly" in low or "invitation" in low:
                logger.info(f"  PAGE TEXT: {line.strip()[:120]}")

        # Check shadow DOM text too
        shadow_text = driver.execute_script("""
            const results = [];
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                if (!el.shadowRoot) continue;
                const text = (el.shadowRoot.textContent || '').toLowerCase();
                if (text.includes('limit') || text.includes('weekly') || text.includes('invitation')) {
                    results.push({
                        host: el.tagName + '.' + (el.className||'').substring(0,30),
                        snippet: text.substring(0, 300)
                    });
                }
            }
            return JSON.stringify(results, null, 2);
        """)
        logger.info(f"  Shadow DOM matches: {shadow_text}")

        screenshot(driver, "03_final_state")

        # ── Verdict ──────────────────────────────────────────────
        if result == "weekly_limit":
            logger.info("✅ PASS — _send_connection_with_note returned 'weekly_limit'")
        elif detected:
            logger.info("⚠️  PARTIAL — manual check found it but send flow didn't return 'weekly_limit'")
        else:
            logger.info("❌ FAIL — weekly limit message NOT detected (check screenshots)")

        logger.info("=" * 60)
        logger.info("Browser stays open 60s for inspection …")
        logger.info("=" * 60)
        time.sleep(60)

    except Exception:
        logger.error(f"Fatal:\n{traceback.format_exc()}")
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()

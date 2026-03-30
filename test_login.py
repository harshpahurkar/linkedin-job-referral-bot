"""Quick login-only test — verifies browser launch + LinkedIn auth."""

import time
import traceback
from config import Config
from utils import get_logger, create_driver, clear_session_cookies
from auth import login
from antidetect import reset_session, check_for_linkedin_warnings

logger = get_logger("test_login")

def main():
    # Force headless OFF so we can watch
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    issues = Config.validate()
    if issues:
        for i in issues:
            logger.error(f"Config issue: {i}")
        return

    driver = None
    try:
        logger.info("=" * 50)
        logger.info("🧪 LOGIN TEST — browser will open visibly")
        logger.info("=" * 50)

        logger.info("🌐 Launching Chrome …")
        driver = create_driver()
        logger.info("✅ Browser launched successfully.")

        # Quick sanity check — can Chrome navigate at all?
        logger.info("🔍 Sanity check: navigating to google.com …")
        driver.get("https://www.google.com")
        time.sleep(2)
        logger.info(f"   URL: {driver.current_url}")
        logger.info(f"   Title: {driver.title}")

        reset_session()

        logger.info("🔐 Attempting LinkedIn login …")
        if not login(driver):
            logger.error("❌ LOGIN FAILED. Check credentials in .env")
            # Still keep browser open so user can see what happened
            logger.info(f"   Final URL: {driver.current_url}")
            logger.info(f"   Final title: {driver.title}")
            logger.info("Browser stays open 60s so you can inspect …")
            time.sleep(60)
            return

        # Check for LinkedIn warnings post-login
        warning, reason = check_for_linkedin_warnings(driver)
        if warning:
            logger.warning(f"⚠️  LinkedIn warning detected: {reason}")
        else:
            logger.info("✅ No LinkedIn warnings detected.")

        logger.info("=" * 50)
        logger.info("✅ ALL GOOD — Login successful, session is valid.")
        logger.info(f"   Current URL: {driver.current_url}")
        logger.info(f"   Page title:  {driver.title}")
        logger.info("=" * 50)

        # Keep browser open so user can inspect
        logger.info("Browser will stay open for 60 seconds so you can look around …")
        logger.info("(Press Ctrl+C to close early)")
        time.sleep(60)

    except KeyboardInterrupt:
        logger.info("Interrupted — closing.")
    except Exception as e:
        logger.error(f"Test failed: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
    finally:
        if driver:
            try:
                clear_session_cookies(driver)
            except Exception:
                pass
            driver.quit()
            logger.info("Browser closed.")

if __name__ == "__main__":
    main()

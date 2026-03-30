"""
Standalone Post Hunter pipeline — finds hiring posts on LinkedIn,
scores them, and sends connection requests to legit posters.

Runs independently from the main referral bot so you can launch
it whenever you want.

Usage:
    python run_post_hunt.py          (headless)
    python run_post_hunt.py --show   (visible Chrome for debugging)
"""

import subprocess
import sys
import time

from config import Config
from models import Database
from utils import get_logger, create_driver, cleanup_driver, purge_old_profiles
from auth import login
from post_hunter import hunt_hiring_posts

logger = get_logger("run_post_hunt")


def _cleanup_stale_chrome():
    """Kill zombie Chrome processes from previous bot runs.

    Only kills Chrome instances using the bot's 'Chrome Bot Data'
    profile directory.  User's normal browser windows are left alone.
    """
    try:
        result = subprocess.run(
            [
                "wmic", "process", "where",
                "name='chrome.exe' and commandline like '%chrome-bot-profiles%'",
                "get", "processid",
            ],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            pid = line.strip()
            if pid.isdigit():
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, text=True, timeout=5,
                )
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chromedriver.exe"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass
    time.sleep(2)


def main():
    show_browser = "--show" in sys.argv

    logger.info("=" * 60)
    logger.info("🔍 POST HUNTER — Standalone Pipeline")
    logger.info("=" * 60)

    if show_browser:
        Config.HEADLESS = False
        logger.info("👁️  Chrome will be visible (--show flag)")
    else:
        logger.info(f"🖥️  HEADLESS={Config.HEADLESS}")

    logger.info(
        f"📊 Settings: max {Config.POST_HUNT_MAX_PER_RUN}/run, "
        f"min score {Config.POST_HUNT_MIN_SCORE}, "
        f"keywords pool: {len(Config.POST_HUNT_KEYWORDS)}"
    )

    db = Database()
    driver = None

    try:
        _cleanup_stale_chrome()
        purge_old_profiles()

        logger.info("🌐 Launching Chrome …")
        driver = create_driver()

        logger.info("🔐 Logging into LinkedIn …")
        if not login(driver):
            logger.error("❌ Login failed. Aborting.")
            return

        # Small warmup — visit feed briefly
        logger.info("🏃 Warmup …")
        driver.get("https://www.linkedin.com/feed/")
        time.sleep(3)

        # Run the post hunter
        logger.info("🔍 Starting post hunt …")
        engaged = hunt_hiring_posts(driver, db)

        logger.info("=" * 60)
        logger.info(f"🏁 Done: {engaged} engagements")
        logger.info("=" * 60)

        if show_browser:
            try:
                input("\n[PAUSE] Press Enter to close the browser...")
            except (EOFError, UnicodeEncodeError, UnicodeDecodeError):
                pass

    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        error_msg = str(e)
        if Config.LINKEDIN_PASSWORD and Config.LINKEDIN_PASSWORD in error_msg:
            error_msg = error_msg.replace(Config.LINKEDIN_PASSWORD, "********")
        logger.error(f"Error: {error_msg}")
        if show_browser:
            try:
                input("\n[PAUSE] Press Enter to close the browser...")
            except (EOFError, UnicodeEncodeError, UnicodeDecodeError):
                pass
    finally:
        if driver:
            cleanup_driver(driver)
        db.close()


if __name__ == "__main__":
    main()

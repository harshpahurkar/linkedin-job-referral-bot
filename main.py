"""
LinkedIn Job Referral Bot — Main entry point.

Usage:
    python main.py              Run once immediately
    python main.py --schedule   Run daily on a schedule
    python main.py --dry-run    Scrape only, no messages sent
"""

import argparse
import random
import subprocess
import sys
import time

from config import Config
from models import Database
from utils import get_logger, create_driver, human_delay, scroll_page, simulate_random_mouse_movement
from auth import login
from scraper import scrape_jobs
from messenger import find_and_message_employees
from scheduler import start_scheduler

logger = get_logger("main")


def _warmup_browse(driver):
    """Spend 30–90 seconds browsing LinkedIn like a normal user.

    Real humans check their feed, scroll around, maybe glance at
    notifications before searching for jobs. This warm-up makes the
    session look organic.
    """
    logger.info("🏃 Warm-up: browsing feed & notifications …")

    try:
        # 1. Scroll the feed for a bit
        driver.get("https://www.linkedin.com/feed/")
        human_delay(2, 4)
        for _ in range(random.randint(2, 4)):
            scroll_page(driver, scrolls=1)
            simulate_random_mouse_movement(driver)
            human_delay(2, 5)

        # 2. Check notifications (70% of the time)
        if random.random() < 0.70:
            driver.get("https://www.linkedin.com/notifications/")
            human_delay(2, 4)
            scroll_page(driver, scrolls=random.randint(1, 2))
            human_delay(1, 3)

        # 3. Maybe glance at messaging (40% of the time)
        if random.random() < 0.40:
            driver.get("https://www.linkedin.com/messaging/")
            human_delay(2, 5)

        logger.info("✅ Warm-up complete.")
    except Exception as e:
        logger.debug(f"Warm-up browsing failed (non-critical): {e}")


def _cleanup_stale_chrome():
    """Kill any leftover Chrome/ChromeDriver processes from previous runs.

    Without this, Selenium can't connect to the bot's Chrome profile
    because the old process still holds the lock file.
    """
    logger.info("🧹 Cleaning up stale Chrome processes …")
    killed = False
    for proc in ("chrome.exe", "chromedriver.exe"):
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", proc],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                killed = True
                logger.info(f"   Killed leftover {proc}")
        except Exception:
            pass  # process not found = fine
    if killed:
        time.sleep(2)  # give OS time to release file locks
    else:
        logger.info("   No stale processes found — clean start.")


def run_pipeline(dry_run: bool = False):
    """Execute the full scrape → message pipeline once."""
    logger.info("=" * 60)
    logger.info("🚀 Starting LinkedIn Job Referral Bot")
    logger.info("=" * 60)

    # Validate config
    issues = Config.validate()
    if issues:
        for issue in issues:
            logger.error(f"Config issue: {issue}")
        logger.error("Fix your .env file and try again.")
        return

    db = Database()
    driver = None

    try:
        # 0. Weekly safety check
        weekly_conns = db.weekly_connections_sent()
        weekly_views = db.weekly_profiles_viewed()
        logger.info(
            f"📊 Weekly activity: {weekly_conns}/{Config.MAX_CONNECTIONS_PER_WEEK} "
            f"connections, {weekly_views}/{Config.MAX_PROFILE_VIEWS_PER_WEEK} profile views"
        )
        if weekly_conns >= Config.MAX_CONNECTIONS_PER_WEEK:
            logger.warning(
                "🛑 Weekly connection limit already hit. Skipping this run to protect your account."
            )
            db.log_run(jobs_found=0, messages_sent=0)
            return

        # 1. Pick a random daily target (looks human — different volume each day)
        daily_target = random.randint(Config.DAILY_TARGET_MIN, Config.DAILY_TARGET_MAX)
        Config.MAX_MESSAGES_PER_DAY = daily_target
        est_companies = daily_target // Config.MAX_MESSAGES_PER_COMPANY
        logger.info(
            f"🎲 Today's random target: {daily_target} connections "
            f"(~{est_companies} companies × {Config.MAX_MESSAGES_PER_COMPANY} people each)"
        )
        remaining_weekly = Config.MAX_CONNECTIONS_PER_WEEK - weekly_conns
        if daily_target > remaining_weekly:
            daily_target = remaining_weekly
            Config.MAX_MESSAGES_PER_DAY = daily_target
            logger.info(f"⚠️  Capped to {daily_target} to stay under weekly limit.")

        # 2. Clear old jobs for a fresh scrape each run
        db.clear_jobs()
        logger.info("🗑️  Cleared old job data — starting fresh scrape.")

        # 3. Kill stale Chrome, then launch browser & log in
        _cleanup_stale_chrome()
        logger.info("🌐 Launching browser …")
        driver = create_driver()

        logger.info("🔐 Logging into LinkedIn …")
        if not login(driver):
            logger.error("Could not log in. Aborting.")
            return

        # ── Anti-detection: warm-up browsing ──────────────────────
        # Mimic a real user who checks their feed & notifications
        # before doing anything productive. Takes 30-90 seconds.
        _warmup_browse(driver)

        # 4. Scrape jobs
        logger.info("📋 Scraping job listings …")
        new_jobs = scrape_jobs(driver, db)

        logger.info("─" * 50)
        logger.info(f"📊 Scrape results: {len(new_jobs)} jobs selected for outreach")
        if new_jobs:
            companies = {j.company for j in new_jobs}
            logger.info(f"   Companies: {len(companies)} unique")
            for j in new_jobs:
                logger.info(f"   • {j.title}  @  {j.company}  ({j.location})")
        logger.info("─" * 50)

        if dry_run:
            logger.info("🏁 Dry run — skipping outreach. Jobs saved to DB.")
            db.log_run(jobs_found=len(new_jobs), messages_sent=0)
            return

        # 5. Find employees & send referral messages
        if new_jobs:
            logger.info("✉️  Starting referral outreach …")
            msgs_sent = find_and_message_employees(driver, db, new_jobs)
        else:
            logger.info("No new jobs to process for outreach.")
            msgs_sent = 0

        db.log_run(jobs_found=len(new_jobs), messages_sent=msgs_sent)
        logger.info("🏁 Pipeline complete.")

    except KeyboardInterrupt:
        import traceback
        logger.info("Interrupted by user.")
        logger.info(f"KeyboardInterrupt traceback:\n{traceback.format_exc()}")
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
    finally:
        if driver:
            driver.quit()
            logger.info("Browser closed.")
        db.close()


def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn Job Referral Bot — scrape jobs & request referrals"
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run on a daily schedule instead of once",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape jobs only, don't send any messages",
    )
    args = parser.parse_args()

    if args.schedule:
        logger.info("Starting in SCHEDULED mode …")
        start_scheduler(lambda: run_pipeline(dry_run=args.dry_run))
    else:
        run_pipeline(dry_run=args.dry_run)


if __name__ == "__main__":
    main()

"""
LinkedIn Job Referral Bot — Main entry point.

Usage:
    python main.py              Run once immediately
    python main.py --schedule   Run daily on a schedule
    python main.py --dry-run    Scrape only, no messages sent
    python main.py --humanized-start   Enable skip-day and startup jitter
"""

import argparse
import random
import subprocess
import sys
import time
from datetime import datetime

from config import Config
from models import Database
from utils import get_logger, create_driver, cleanup_driver, purge_old_profiles, human_delay, scroll_page, simulate_random_mouse_movement
from auth import login
from scraper import scrape_jobs, scrape_jobs_by_window
from messenger import find_and_message_employees
from scheduler import start_scheduler
from antidetect import (
    reset_session, get_session, is_session_safe,
    check_for_linkedin_warnings, simulate_natural_break,
)

logger = get_logger("main")


# ── Human-like day/week patterns ─────────────────────────────────────
# Real people don't use LinkedIn 7 days a week, every week.
# These add skip-day logic and weekend volume reduction.

def _should_skip_today() -> tuple[bool, str]:
    """Decide whether to skip today's run entirely.

    Returns (should_skip, reason).

    Strategy:
      - Weekdays: 10% skip chance (people take a day off)
      - Saturday: 45% skip chance
      - Sunday:   55% skip chance
      - Random "vacation" day: extra 5% on top (simulates being away)

    Over a month, this means ~3-4 weekday skips and ~4 weekend skips,
    which looks like a real human's LinkedIn usage pattern.
    """
    today = datetime.now()
    day_name = today.strftime("%A")
    day_of_week = today.weekday()  # 0=Mon, 6=Sun

    # Base skip probability by day type
    if day_of_week == 5:       # Saturday
        skip_prob = 0.45
    elif day_of_week == 6:     # Sunday
        skip_prob = 0.55
    else:                      # Weekday
        skip_prob = 0.10

    # Extra "vacation/sick day" probability
    skip_prob += 0.05

    roll = random.random()
    if roll < skip_prob:
        return True, f"{day_name} skip (roll={roll:.2f} < prob={skip_prob:.2f})"
    return False, ""


def _weekend_volume_adjustment(base_min: int, base_max: int) -> tuple[int, int]:
    """Reduce daily target on weekends (humans are less active).

    Weekdays: full volume (base_min–base_max)
    Saturday: 40-60% of normal
    Sunday:   30-50% of normal
    """
    day_of_week = datetime.now().weekday()

    if day_of_week == 5:       # Saturday
        factor = random.uniform(0.40, 0.60)
    elif day_of_week == 6:     # Sunday
        factor = random.uniform(0.30, 0.50)
    else:
        return base_min, base_max

    new_min = max(3, int(base_min * factor))
    new_max = max(new_min + 2, int(base_max * factor))
    return new_min, new_max


def _warmup_browse(driver):
    """Spend 30–90 seconds browsing LinkedIn like a normal user.

    Real humans check their feed, scroll around, maybe glance at
    notifications before searching for jobs. This warm-up makes the
    session look organic.

    IMPORTANT: warm-up must last 2-5 minutes.  A session that goes
    straight from login to job-search in 30 seconds is a bot signal.
    """
    from antidetect import natural_scroll_pattern, _maybe_like_feed_post, safe_get

    warmup_duration = random.uniform(120, 300)  # 2-5 minutes total
    logger.info(f"🏃 Warm-up: browsing feed & notifications (~{warmup_duration/60:.1f} min) …")
    start = time.time()

    try:
        # 1. Scroll the feed for a bit + maybe like a post
        safe_get(driver, "https://www.linkedin.com/feed/")
        human_delay(3, 6)
        for _ in range(random.randint(3, 5)):
            scroll_page(driver, scrolls=1)
            simulate_random_mouse_movement(driver)
            human_delay(2, 5)
        _maybe_like_feed_post(driver)
        human_delay(3, 8)

        # 2. Check notifications (70% of the time)
        if random.random() < 0.70 and (time.time() - start) < warmup_duration:
            safe_get(driver, "https://www.linkedin.com/notifications/")
            human_delay(3, 6)
            scroll_page(driver, scrolls=1)
            human_delay(2, 5)

        # 3. Maybe glance at messaging (40% of the time)
        if random.random() < 0.40 and (time.time() - start) < warmup_duration:
            safe_get(driver, "https://www.linkedin.com/messaging/")
            human_delay(4, 8)

        # 4. Check My Network (35% of the time)
        if random.random() < 0.35 and (time.time() - start) < warmup_duration:
            safe_get(driver, "https://www.linkedin.com/mynetwork/")
            human_delay(3, 6)
            scroll_page(driver, scrolls=random.randint(1, 3))
            human_delay(2, 5)

        # 5. Check who viewed your profile (30%)
        if random.random() < 0.30 and (time.time() - start) < warmup_duration:
            safe_get(driver, "https://www.linkedin.com/me/profile-views/")
            human_delay(4, 8)
            scroll_page(driver, scrolls=1)
            human_delay(2, 4)

        # 6. Maybe check your own profile (20%)
        if random.random() < 0.20 and (time.time() - start) < warmup_duration:
            safe_get(driver, "https://www.linkedin.com/in/me/")
            human_delay(3, 6)
            scroll_page(driver, scrolls=random.randint(1, 2))
            human_delay(2, 4)

        # 7. Idle remainder if warmup hasn't hit target duration
        elapsed = time.time() - start
        if elapsed < warmup_duration * 0.6:
            idle_time = random.uniform(warmup_duration * 0.3, warmup_duration - elapsed)
            logger.info(f"  💤 Idling for {idle_time:.0f}s (mimicking reading)")
            time.sleep(idle_time)

        logger.info("✅ Warm-up complete.")
    except Exception as e:
        logger.debug(f"Warm-up browsing failed (non-critical): {e}")


def _cleanup_stale_chrome():
    """Kill leftover Chrome/ChromeDriver from previous bot runs.

    Only kills processes whose command line contains 'Chrome Bot Data'
    (the bot's dedicated profile directory).  User's normal Chrome
    windows are never touched.
    """
    logger.info("🧹 Cleaning up stale bot Chrome processes …")
    killed = False
    try:
        import subprocess
        # WMIC finds Chrome processes by their command-line args
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
                killed = True
                logger.info(f"   Killed bot Chrome PID {pid}")
    except Exception:
        pass
    # Also clean up orphaned chromedriver.exe (always safe)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chromedriver.exe"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass
    if killed:
        time.sleep(2)
    else:
        logger.info("   No stale bot processes found — clean start.")


def run_pipeline(dry_run: bool = False, force_now: bool = True):
    """Execute the full scrape → message pipeline once."""
    logger.info("=" * 60)
    logger.info("🚀 Starting LinkedIn Job Referral Bot")
    logger.info("=" * 60)

    if force_now:
        logger.info("⚡ Immediate start: skipping skip-day and startup jitter.")
    else:
        # ── Human pattern: skip some days entirely ────────────────────
        skip, skip_reason = _should_skip_today()
        if skip:
            logger.info(f"😴 Skipping today's run — {skip_reason}")
            logger.info("   (Real humans don't use LinkedIn every single day.)")
            return

        # ── Human pattern: randomize start time ───────────────────────
        # A bot that starts at exactly 08:00:00 every day is obvious.
        # Add a random 0–25 minute delay so start time varies daily.
        jitter = random.uniform(0, 25 * 60)  # 0-25 minutes in seconds
        logger.info(f"⏳ Start-time jitter: waiting {jitter/60:.1f} min before beginning …")
        time.sleep(jitter)

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
        #    Weekend adjustment: reduce volume on Sat/Sun to match real behavior.
        day_min, day_max = _weekend_volume_adjustment(
            Config.DAILY_TARGET_MIN, Config.DAILY_TARGET_MAX,
        )
        daily_target = random.randint(day_min, day_max)
        Config.MAX_MESSAGES_PER_DAY = daily_target
        est_companies = daily_target // Config.MAX_MESSAGES_PER_COMPANY
        day_name = datetime.now().strftime("%A")
        logger.info(
            f"🎲 Today's target ({day_name}): {daily_target} connections "
            f"(range {day_min}–{day_max}, "
            f"~{est_companies} companies × {Config.MAX_MESSAGES_PER_COMPANY} people each)"
        )
        remaining_weekly = Config.MAX_CONNECTIONS_PER_WEEK - weekly_conns
        if daily_target > remaining_weekly:
            daily_target = remaining_weekly
            Config.MAX_MESSAGES_PER_DAY = daily_target
            logger.info(f"⚠️  Capped to {daily_target} to stay under weekly limit.")

        # 2. Clear old jobs for a fresh scrape each run
        db.clear_jobs()
        logger.info("🗑️  Cleared old job data — starting fresh scrape.")

        # 3. Kill stale Chrome, purge old profiles, then launch browser & log in
        _cleanup_stale_chrome()
        purge_old_profiles()
        logger.info("🌐 Launching browser …")
        driver = create_driver()

        # ── Initialise anti-detection session tracker ─────────────
        reset_session()
        session = get_session()
        logger.info(
            f"🛡️  Anti-detection: session tracker active | "
            f"hourly caps: {session.MAX_CONNECTIONS_PER_HOUR} conn, "
            f"{session.MAX_PROFILES_PER_HOUR} profiles | "
            f"break every {session.ACTIONS_BEFORE_BREAK} actions"
        )

        logger.info("🔐 Logging into LinkedIn …")
        if not login(driver):
            logger.error("Could not log in. Aborting.")
            return

        # ── Check for warnings immediately after login ────────────
        warning, reason = check_for_linkedin_warnings(driver)
        if warning:
            logger.critical(f"🚨 LinkedIn warning on login: {reason}")
            logger.critical("🛑 ABORTING — do NOT run the bot until this is resolved!")
            db.log_run(jobs_found=0, messages_sent=0)
            return

        # ── Anti-detection: warm-up browsing ──────────────────────
        _warmup_browse(driver)

        # ── Post-warmup safety check ──────────────────────────────
        if not is_session_safe():
            logger.critical("🛑 Warning detected during warmup. Aborting.")
            db.log_run(jobs_found=0, messages_sent=0)
            return

        # ── Dry-run mode: scrape everything, no messages ─────────
        if dry_run:
            logger.info("📋 Scraping job listings (dry run) …")
            new_jobs = scrape_jobs(driver, db)
            logger.info("─" * 50)
            logger.info(f"📊 Scrape results: {len(new_jobs)} jobs selected for outreach")
            if new_jobs:
                companies = {j.company for j in new_jobs}
                logger.info(f"   Companies: {len(companies)} unique")
                for j in new_jobs:
                    logger.info(f"   • {j.title}  @  {j.company}  ({j.location})")
            logger.info("─" * 50)
            logger.info("🏁 Dry run — skipping outreach & post hunt. Jobs saved to DB.")
            db.log_run(jobs_found=len(new_jobs), messages_sent=0)
            return

        # ── INTERLEAVED PIPELINE ──────────────────────────────────
        # Scrape one time window → message those jobs → repeat.
        # This is much faster than scrape-all-then-message because:
        #   1. Fresh 1-hour jobs often fill the daily target alone.
        #   2. We stop scraping the moment enough messages are sent.
        #   3. No wasted time scraping 7 windows when 2 suffice.
        logger.info("📋 Starting interleaved scrape → outreach pipeline …")
        total_jobs_found = 0
        total_msgs_sent = 0
        first_batch = True

        for batch in scrape_jobs_by_window(driver, db):
            total_jobs_found += len(batch)

            logger.info("─" * 50)
            logger.info(
                f"📊 Batch: {len(batch)} jobs | "
                f"Running total: {total_jobs_found} jobs, {total_msgs_sent} messages"
            )
            if batch:
                companies = {j.company for j in batch}
                logger.info(f"   Companies: {len(companies)} unique")
                for j in batch:
                    logger.info(f"   • {j.title}  @  {j.company}  ({j.location})")
            logger.info("─" * 50)

            # ── Safety check before outreach ──────────────────────
            if not is_session_safe():
                logger.critical("🛑 Warning detected. Aborting outreach.")
                break

            # ── Natural break between scraping and messaging ──────
            # First batch gets a full break (transition from browsing
            # jobs → messaging). Subsequent batches get a shorter one
            # since we're already in the messaging flow.
            if first_batch:
                simulate_natural_break(driver)
                first_batch = False
            else:
                human_delay(5, 12)  # brief pause between windows

            if not is_session_safe():
                logger.critical("🛑 Warning detected during break. Aborting.")
                break

            # ── Message this batch ────────────────────────────────
            if batch:
                logger.info("✉️  Outreach for this batch …")
                batch_sent = find_and_message_employees(driver, db, batch)
                total_msgs_sent += batch_sent
                logger.info(
                    f"📬 Batch done: {batch_sent} sent this batch, "
                    f"{total_msgs_sent} total"
                )

            # ── Check if daily target is hit ──────────────────────
            if total_msgs_sent >= daily_target:
                logger.info(
                    f"🎯 Daily target reached ({total_msgs_sent}/{daily_target})! "
                    f"Skipping remaining time windows."
                )
                break

        db.log_run(jobs_found=total_jobs_found, messages_sent=total_msgs_sent)
        logger.info(
            f"🏁 Pipeline complete: {total_jobs_found} jobs found, "
            f"{total_msgs_sent} messages sent."
        )

    except KeyboardInterrupt:
        import traceback
        logger.info("Interrupted by user.")
        logger.info(f"KeyboardInterrupt traceback:\n{traceback.format_exc()}")
    except Exception as e:
        # Sanitize: don't let credentials leak into logs via traceback
        error_msg = str(e)
        if Config.LINKEDIN_PASSWORD and Config.LINKEDIN_PASSWORD in error_msg:
            error_msg = error_msg.replace(Config.LINKEDIN_PASSWORD, "********")
        logger.error(f"Pipeline error: {error_msg}")
    finally:
        if driver:
            cleanup_driver(driver)
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
    parser.add_argument(
        "--humanized-start",
        action="store_true",
        help="Enable skip-day and random startup jitter before run",
    )
    args = parser.parse_args()

    if args.schedule:
        logger.info("Starting in SCHEDULED mode …")
        # Scheduled mode keeps humanized startup behavior by default.
        start_scheduler(lambda: run_pipeline(dry_run=args.dry_run, force_now=False))
    else:
        run_pipeline(dry_run=args.dry_run, force_now=not args.humanized_start)


if __name__ == "__main__":
    main()

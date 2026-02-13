"""
Scheduler — runs the scrape + message pipeline daily using APScheduler.
Randomizes the run time each day so LinkedIn can't pattern-match a fixed schedule.
"""

import random
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from config import Config
from utils import get_logger

logger = get_logger("scheduler")


def start_scheduler(run_fn):
    """
    Start a blocking scheduler that triggers `run_fn` every day
    at a RANDOMIZED time around the configured hour.
    
    Each day it picks a random minute offset (-90 to +90 min)
    from the base hour, so the bot never runs at the exact same time.
    """
    scheduler = BlockingScheduler()

    # Use jitter: APScheduler's cron trigger supports jitter (in seconds)
    # This adds a random delay of 0 to jitter seconds EACH time the trigger fires.
    # 5400 seconds = 90 minutes of random spread around the configured time.
    trigger = CronTrigger(
        hour=Config.DAILY_RUN_HOUR,
        minute=Config.DAILY_RUN_MINUTE,
        jitter=5400,  # random ±90 min spread
    )

    scheduler.add_job(
        run_fn,
        trigger=trigger,
        id="linkedin_daily_job",
        name="LinkedIn Daily Scrape & Outreach",
        max_instances=1,
        replace_existing=True,
    )

    earliest = max(0, Config.DAILY_RUN_HOUR * 60 + Config.DAILY_RUN_MINUTE)
    latest_min = earliest + 90
    logger.info(
        f"⏰ Scheduler started — runs daily around "
        f"{Config.DAILY_RUN_HOUR:02d}:{Config.DAILY_RUN_MINUTE:02d} "
        f"(±90 min random jitter, so roughly "
        f"{earliest // 60:02d}:{earliest % 60:02d}–{latest_min // 60:02d}:{latest_min % 60:02d})"
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shut down.")
        scheduler.shutdown()

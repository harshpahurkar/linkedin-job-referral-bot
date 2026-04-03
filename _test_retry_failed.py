"""
Test script: retry the contacts that failed with the BMP/emoji error.

Loads the 10 failed contacts from the DB, pairs each with its job,
generates a message via _pick_message, and calls _send_connection_with_note.
Stops after the first 3 successes so we don't burn through all of them.
"""

import sys
import time
import traceback

from config import Config
from models import Database, Job, Contact
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session
from utils import human_delay
from messenger import _send_connection_with_note, _pick_message

logger = get_logger("test_retry_failed")

# ── Remaining contacts from the BMP error batch ──
FAILED_CONTACTS = [
    # (name, first_name, profile_url, company, title, job_company_like)
    ("Hussein Elguindi", "Hussein", "https://www.linkedin.com/in/hussein-elguindi", "Cloudflare", "SWE @ Cloudflare", "Cloudflare"),
    ("Adnan Bhuiyan", "Adnan", "https://www.linkedin.com/in/adnan-bhuiyan", "Toast", "Senior Software Engineer", "Toast"),
    ("Luiz de Paula", "Luiz", "https://www.linkedin.com/in/luizfreitasdepaula", "Toast", "Staff Software Engineer", "Toast"),
    ("Thomas Mathers", "Thomas", "https://www.linkedin.com/in/tom-mathers", "Toast", "Senior Software Engineer @ Toast", "Toast"),
]

MAX_SUCCESSES = 3  # stop after this many successful sends


def main():
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    issues = Config.validate()
    if issues:
        for i in issues:
            logger.error(f"Config issue: {i}")
        return

    db = Database()
    driver = None

    try:
        logger.info("=" * 50)
        logger.info("🧪 RETRY TEST — re-sending to BMP-error contacts")
        logger.info("=" * 50)

        driver = create_driver()
        reset_session()

        logger.info("🔐 Logging in …")
        if not login(driver):
            logger.error("❌ Login failed")
            return

        logger.info("✅ Logged in. Starting retry loop …")
        time.sleep(2)

        successes = 0
        failures = 0

        for name, first_name, url, company, title, job_company_like in FAILED_CONTACTS:
            if successes >= MAX_SUCCESSES:
                logger.info(f"🛑 Hit {MAX_SUCCESSES} successes — stopping test.")
                break

            # Skip if already messaged (in case we re-run)
            contact_id = url.rstrip("/").split("/")[-1]
            if db.already_messaged(contact_id):
                logger.info(f"  ⏭ Already messaged {name}, skipping.")
                continue

            # Find the associated job
            row = db.conn.execute(
                "SELECT * FROM jobs WHERE company LIKE ? LIMIT 1",
                (f"%{job_company_like}%",),
            ).fetchone()

            if not row:
                logger.warning(f"  ⚠️  No job found for {company}, skipping {name}")
                continue

            job = Job(
                job_id=row["job_id"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                url=row["url"],
                description=row["description"],
                date_scraped=row["date_scraped"],
            )

            contact = Contact(
                contact_id=contact_id,
                name=name,
                first_name=first_name,
                profile_url=url,
                company=company,
                title=title,
            )

            message = _pick_message(contact, job)
            logger.info(f"📨 Trying {name} @ {company} …")
            logger.info(f"   Message ({len(message)} chars): {message[:80]}…")

            result = _send_connection_with_note(driver, contact, message)
            if result in ("connection_sent", "dm_sent"):
                successes += 1
                db.mark_messaged(contact.contact_id)
                logger.info(f"  ✅ {result} to {name} [{successes}/{MAX_SUCCESSES}]")
            else:
                failures += 1
                logger.warning(f"  ❌ Failed for {name} (result={result})")

            human_delay(5, 10)

        logger.info("=" * 50)
        logger.info(f"🏁 Done: {successes} sent, {failures} failed")
        logger.info("=" * 50)

        logger.info("Browser stays open 30s for inspection …")
        time.sleep(30)

    except Exception:
        logger.error(f"Fatal error:\n{traceback.format_exc()}")
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()

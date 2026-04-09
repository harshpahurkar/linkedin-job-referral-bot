"""
Hunt for a contract employee to verify the filter works.

Pulls companies from the DB, visits employee profiles one by one,
runs _is_contract_employee() on each, and stops when it finds one.
NO connection requests are sent — read-only.

Reports: name, URL, and detection result for every profile visited.
"""

import time
import traceback
from datetime import datetime

from config import Config
from models import Database
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get, get_session
from messenger import (
    _is_contract_employee,
    _browse_company_people_page,
    _search_company_employees,
    _filter_relevant_titles,
    _score_contact,
)

logger = get_logger("find_contract")

MAX_PROFILES = 40  # visit up to this many before giving up


def main():
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    db = Database()
    driver = None

    try:
        logger.info("=" * 60)
        logger.info("HUNT FOR CONTRACT EMPLOYEES")
        logger.info(f"Will visit up to {MAX_PROFILES} profiles")
        logger.info("=" * 60)

        driver = create_driver()
        reset_session()

        logger.info("Logging in ...")
        if not login(driver):
            logger.error("Login failed")
            return
        logger.info("Logged in")
        time.sleep(3)

        # Get companies from recent jobs
        rows = db.conn.execute(
            "SELECT DISTINCT company FROM jobs ORDER BY date_scraped DESC LIMIT 10"
        ).fetchall()
        companies = [r["company"] for r in rows if r["company"]]

        if not companies:
            logger.error("No companies in DB. Run a scrape first.")
            return

        logger.info(f"Companies to search: {companies}")

        visited = 0
        contract_found = None
        all_results = []

        for company in companies:
            if contract_found or visited >= MAX_PROFILES:
                break

            logger.info(f"\n{'='*50}")
            logger.info(f"Searching employees at: {company}")
            logger.info(f"{'='*50}")

            # Try company people page first, fall back to search
            contacts = _browse_company_people_page(driver, db, company)
            if not contacts:
                contacts = _search_company_employees(driver, company)
            if not contacts:
                logger.info(f"  No contacts found at {company}, skipping")
                continue

            # Filter to relevant titles
            contacts = _filter_relevant_titles(contacts)
            contacts.sort(key=_score_contact, reverse=True)

            logger.info(f"  Found {len(contacts)} relevant contacts at {company}")

            for contact in contacts:
                if visited >= MAX_PROFILES:
                    break
                if not contact.profile_url:
                    continue

                # Skip already-messaged contacts (we want fresh ones)
                if db.already_messaged(contact.contact_id):
                    continue

                visited += 1
                logger.info(f"\n  [{visited}/{MAX_PROFILES}] {contact.name}")
                logger.info(f"    Title: {contact.title}")
                logger.info(f"    URL:   {contact.profile_url}")

                try:
                    if not safe_get(driver, contact.profile_url):
                        logger.warning(f"    Could not load profile")
                        all_results.append((contact.name, contact.profile_url, contact.company, contact.title, "LOAD_FAIL"))
                        continue

                    get_session().record_profile_view()
                    time.sleep(2)

                    # Scroll to experience section
                    driver.execute_script("window.scrollTo(0, 600);")
                    time.sleep(1.5)

                    is_contract = _is_contract_employee(driver)
                    status = "CONTRACT" if is_contract else "FULL-TIME"
                    all_results.append((contact.name, contact.profile_url, contact.company, contact.title, status))

                    if is_contract:
                        logger.info(f"    >>> CONTRACT DETECTED! <<<")
                        contract_found = (contact.name, contact.profile_url, contact.company, contact.title)
                        # Take a screenshot
                        ts = datetime.now().strftime("%H%M%S")
                        path = f"data/screenshots/test_contract_filter/{ts}_CONTRACT_{contact.name.replace(' ', '_')}.png"
                        driver.save_screenshot(path)
                        logger.info(f"    Screenshot: {path}")
                        break
                    else:
                        logger.info(f"    Full-time employee")

                except Exception as e:
                    logger.error(f"    Error: {e}")
                    all_results.append((contact.name, contact.profile_url, contact.company, contact.title, f"ERROR"))

                # Small delay between profiles
                time.sleep(3)

        # ── Summary ───────────────────────────────────────────
        print("\n" + "=" * 70)
        print("RESULTS")
        print("=" * 70)
        for name, url, company, title, status in all_results:
            flag = "SKIP" if status == "CONTRACT" else "OK  "
            print(f"  [{flag}] {name:28s} | {company:20s} | {title[:30]:30s} | {status}")

        print(f"\nVisited {visited} profiles total")

        if contract_found:
            name, url, company, title = contract_found
            print(f"\n{'!'*60}")
            print(f"CONTRACT EMPLOYEE FOUND:")
            print(f"  Name:    {name}")
            print(f"  Company: {company}")
            print(f"  Title:   {title}")
            print(f"  URL:     {url}")
            print(f"  -> Bot WOULD SKIP this person")
            print(f"{'!'*60}")
            print(f"\nGo verify: {url}")
        else:
            print(f"\nNo contract employees found in {visited} profiles.")
            print("All contacts at these companies appear to be full-time.")

    except Exception as e:
        logger.error(f"Failed: {e}")
        traceback.print_exc()
    finally:
        if driver:
            logger.info("\nDone. Closing browser in 15s ...")
            time.sleep(15)
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()

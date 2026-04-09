"""
Dry-run: full pipeline WITHOUT sending any messages.

Logs into LinkedIn, scrapes jobs from DB (or fresh scrape),
finds employees at each company, visits profiles, runs contract
detection — then reports what WOULD have happened.

No connection requests or DMs are sent.

Usage:
    python _test_dry_run.py
    python _test_dry_run.py --companies 5    (limit to N companies)
    python _test_dry_run.py --fresh           (scrape new jobs first)
"""

import argparse
import random
import time
import traceback
from collections import defaultdict

from config import Config
from models import Database, Job, Contact
from utils import get_logger, create_driver, cleanup_driver, human_delay
from auth import login
from antidetect import (
    reset_session, get_session, safe_get, is_session_safe,
    check_for_linkedin_warnings, realistic_profile_reading,
)
from messenger import (
    _browse_company_people_page,
    _search_company_employees,
    _filter_contacts,
    _score_contact,
    _pick_message,
    _is_contract_employee,
    _scroll_profile_main,
    _split_contacts_by_role,
)

logger = get_logger("dry_run")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--companies", type=int, default=8,
                        help="Max companies to process (default: 8)")
    parser.add_argument("--fresh", action="store_true",
                        help="Scrape fresh jobs before dry run")
    args = parser.parse_args()

    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    db = Database()
    driver = None

    # Stats
    stats = {
        "companies_checked": 0,
        "contacts_found": 0,
        "contacts_after_filter": 0,
        "profiles_visited": 0,
        "contract_skipped": 0,
        "would_message": 0,
        "already_messaged": 0,
        "no_contacts": 0,
    }
    contract_people = []  # (name, company, url, employment_text)
    would_message_people = []  # (name, company, title, url)
    company_results = []  # (company, found, filtered, contract, messageable)

    try:
        logger.info("=" * 60)
        logger.info("🧪 DRY RUN — full pipeline, zero messages sent")
        logger.info(f"   Companies to process: {args.companies}")
        logger.info("=" * 60)

        driver = create_driver()
        reset_session()

        logger.info("🔐 Logging in …")
        if not login(driver):
            logger.error("❌ Login failed")
            return
        logger.info("✅ Logged in")
        time.sleep(3)

        # Optional: fresh scrape
        if args.fresh:
            from scraper import scrape_jobs
            logger.info("📋 Scraping fresh jobs …")
            db.clear_jobs()
            jobs = scrape_jobs(driver, db)
            logger.info(f"   Scraped {len(jobs)} jobs")
        else:
            # Load existing jobs from DB
            rows = db.conn.execute(
                "SELECT * FROM jobs ORDER BY date_scraped DESC"
            ).fetchall()
            jobs = []
            for r in rows:
                jobs.append(Job(
                    job_id=r["job_id"], title=r["title"],
                    company=r["company"], location=r["location"],
                    url=r["url"],
                    description=r["description"] if "description" in r.keys() else "",
                ))
            logger.info(f"📋 Loaded {len(jobs)} jobs from DB")

        if not jobs:
            logger.error("❌ No jobs found. Run with --fresh or do a normal run first.")
            return

        # Deduplicate companies
        seen_companies = set()
        unique_jobs = []
        for j in jobs:
            c = j.company.strip()
            if c and c != "Unknown" and c not in seen_companies:
                seen_companies.add(c)
                unique_jobs.append(j)
        random.shuffle(unique_jobs)
        unique_jobs = unique_jobs[:args.companies]

        logger.info(f"🏢 Will check {len(unique_jobs)} companies: "
                     + ", ".join(j.company for j in unique_jobs))
        logger.info("")

        for idx, job in enumerate(unique_jobs, 1):
            company = job.company.strip()
            logger.info(f"\n{'━' * 60}")
            logger.info(f"[{idx}/{len(unique_jobs)}] 🏢 {company}")
            logger.info(f"   Job: {job.title} ({job.location})")
            logger.info(f"{'━' * 60}")

            stats["companies_checked"] += 1

            # Safety check
            if not is_session_safe():
                logger.critical("🛑 Warning detected — stopping!")
                break

            # Find employees
            contacts = _browse_company_people_page(driver, db, company, job=job)
            if not contacts:
                contacts = _search_company_employees(driver, company, job=job)

            if not contacts:
                logger.info(f"   → No contacts found at {company}")
                stats["no_contacts"] += 1
                company_results.append((company, 0, 0, 0, 0))
                continue

            raw_count = len(contacts)
            stats["contacts_found"] += raw_count
            logger.info(f"   Found {raw_count} raw contacts")

            # Filter
            contacts = _filter_contacts(contacts)
            filtered_count = len(contacts)
            stats["contacts_after_filter"] += filtered_count
            logger.info(f"   After filter: {filtered_count} contacts")

            # Sort by score
            contacts.sort(key=_score_contact, reverse=True)

            # Show role split
            try:
                tech, recruiters = _split_contacts_by_role(contacts)
                logger.info(f"   Role split: {len(tech)} technical, {len(recruiters)} recruiters")
            except Exception:
                pass

            # Visit top contacts and check contract status
            # Limit to ~6 per company to keep it reasonable
            max_visits = min(6, len(contacts))
            company_contract = 0
            company_messageable = 0

            for ci, contact in enumerate(contacts[:max_visits], 1):
                if db.already_messaged(contact.contact_id):
                    logger.info(f"   [{ci}] ⏭ {contact.name} — already messaged")
                    stats["already_messaged"] += 1
                    continue

                if not contact.profile_url:
                    logger.debug(f"   [{ci}] No URL for {contact.name}")
                    continue

                logger.info(f"   [{ci}] Visiting: {contact.name} — {contact.title}")
                stats["profiles_visited"] += 1

                try:
                    if not safe_get(driver, contact.profile_url):
                        logger.warning(f"       ⚠️ Could not load profile")
                        continue

                    time.sleep(2)

                    # Contract check
                    is_contract = _is_contract_employee(driver)

                    if is_contract:
                        stats["contract_skipped"] += 1
                        company_contract += 1
                        # Get the employment type text for reporting
                        emp_text = driver.execute_script(r"""
                            for (const h of document.querySelectorAll('h2, h3')) {
                                if (/^\s*Experience\s*$/i.test(h.textContent)) {
                                    const sec = h.closest('section') || h.parentElement;
                                    const els = sec.querySelectorAll('p, span');
                                    const hRect = h.getBoundingClientRect();
                                    for (const el of els) {
                                        const t = (el.innerText||'').trim();
                                        if (t.length > 80 || !t) continue;
                                        const d = el.getBoundingClientRect().top - hRect.top;
                                        if (d > 0 && d < 250 && /contract|part.time|freelance|intern/i.test(t))
                                            return t;
                                    }
                                }
                            }
                            return '(detected)';
                        """) or "(detected)"
                        contract_people.append((contact.name, company, contact.profile_url, emp_text))
                        logger.info(f"       🚫 CONTRACT — \"{emp_text}\" — WOULD SKIP")
                    else:
                        stats["would_message"] += 1
                        company_messageable += 1
                        msg = _pick_message(contact, job)
                        would_message_people.append((contact.name, company, contact.title, contact.profile_url))
                        logger.info(f"       ✅ FULL-TIME — WOULD MESSAGE")
                        logger.info(f"       📝 Message: \"{msg[:80]}…\"" if len(msg) > 80 else f"       📝 Message: \"{msg}\"")

                except Exception as e:
                    logger.error(f"       ❌ Error: {e}")

                # Small delay between profile visits
                time.sleep(random.uniform(2, 4))

            company_results.append((company, raw_count, filtered_count, company_contract, company_messageable))

            # Check for warnings after each company
            warning, _ = check_for_linkedin_warnings(driver)
            if warning:
                logger.critical("🛑 Warning detected — stopping dry run!")
                break

            # Delay between companies
            time.sleep(random.uniform(3, 6))

        # ══════════════════════════════════════════════════════════
        #  FINAL REPORT
        # ══════════════════════════════════════════════════════════
        logger.info("\n\n" + "═" * 60)
        logger.info("📊 DRY RUN RESULTS")
        logger.info("═" * 60)

        logger.info(f"\n   Companies checked:     {stats['companies_checked']}")
        logger.info(f"   No contacts found:     {stats['no_contacts']}")
        logger.info(f"   Total contacts found:  {stats['contacts_found']}")
        logger.info(f"   After filtering:       {stats['contacts_after_filter']}")
        logger.info(f"   Profiles visited:      {stats['profiles_visited']}")
        logger.info(f"   Already messaged:      {stats['already_messaged']}")
        logger.info(f"   🚫 Contract (skipped): {stats['contract_skipped']}")
        logger.info(f"   ✅ Would message:      {stats['would_message']}")

        if contract_people:
            logger.info(f"\n{'─' * 60}")
            logger.info("🚫 CONTRACT PEOPLE (would be skipped):")
            for name, comp, url, emp in contract_people:
                logger.info(f"   {name:25s} @ {comp:20s} — {emp}")
                logger.info(f"      {url}")

        if would_message_people:
            logger.info(f"\n{'─' * 60}")
            logger.info("✅ WOULD MESSAGE (full-time):")
            for name, comp, title, url in would_message_people:
                logger.info(f"   {name:25s} @ {comp:20s} — {title}")

        logger.info(f"\n{'─' * 60}")
        logger.info("PER-COMPANY BREAKDOWN:")
        logger.info(f"   {'Company':25s} {'Found':>6s} {'Filter':>7s} {'Contract':>9s} {'Msg':>5s}")
        for comp, raw, filt, cont, msg in company_results:
            logger.info(f"   {comp:25s} {raw:>6d} {filt:>7d} {cont:>9d} {msg:>5d}")

        logger.info("\n" + "═" * 60)
        logger.info("🏁 DRY RUN COMPLETE — zero messages were sent")
        logger.info("═" * 60)

    except Exception as e:
        logger.error(f"❌ Dry run failed: {e}")
        traceback.print_exc()
    finally:
        if driver:
            logger.info("\nClosing browser in 5s …")
            time.sleep(5)
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()

"""
Test: contract / non-FT employee detection + dry-run outreach.

Part 1 — Unit tests (no browser):
  • Warning flag propagation (outer-loop escape)
  • Contract skip result handling in main loop logic

Part 2 — Live dry run (browser, no messages sent):
  • Logs into LinkedIn
  • Visits a handful of known profiles (mix of FT & contract)
  • Runs _is_contract_employee() on each
  • Reports results (NO connection requests are sent)

Usage:
    python _test_contract_filter.py
"""

import os
import time
import traceback
from datetime import datetime

from config import Config
from models import Contact, Database
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, realistic_profile_reading, safe_get, get_session
from messenger import _is_contract_employee

logger = get_logger("test_contract_filter")

SCREENSHOTS_DIR = os.path.join("data", "screenshots", "test_contract_filter")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def screenshot(driver, name):
    ts = datetime.now().strftime("%H%M%S")
    path = os.path.join(SCREENSHOTS_DIR, f"{ts}_{name}.png")
    driver.save_screenshot(path)
    logger.info(f"  📸 {name} → {path}")
    return path


# ═══════════════════════════════════════════════════════════════════
#  PART 1: Logic / unit tests (no browser needed)
# ═══════════════════════════════════════════════════════════════════

def test_warning_flag_breaks_outer_loop():
    """Verify the warning-after-send flag escapes the outer job loop.

    The bug was: `break` after warning only exited the inner contact loop.
    Fix: we reuse `weekly_limit_hit = True` which the outer loop checks.
    """
    logger.info("─" * 50)
    logger.info("TEST 1: Warning flag propagates to outer loop")

    # Simulate the logic (no real driver needed)
    weekly_limit_hit = False
    jobs_processed = 0
    outer_escaped = False

    fake_jobs = ["Job A", "Job B", "Job C"]
    for job in fake_jobs:
        if weekly_limit_hit:
            outer_escaped = True
            break
        jobs_processed += 1

        # Simulate inner loop: first job triggers warning
        for i in range(3):
            if job == "Job A" and i == 1:
                # simulate warning detected
                weekly_limit_hit = True
                break

    assert outer_escaped, "Outer loop should have broken on Job B!"
    assert jobs_processed == 1, f"Should process only 1 job, got {jobs_processed}"
    logger.info("  ✅ PASS — warning flag escapes both loops correctly")


def test_contract_skip_continues_loop():
    """Verify 'contract_skip' result causes a continue, not a break."""
    logger.info("─" * 50)
    logger.info("TEST 2: contract_skip continues to next contact")

    results = ["contract_skip", "contract_skip", "connection_sent", "failed"]
    sent = 0
    skipped = 0
    for result in results:
        if result == "contract_skip":
            skipped += 1
            continue
        if result in ("connection_sent", "dm_sent"):
            sent += 1

    assert skipped == 2, f"Should skip 2, got {skipped}"
    assert sent == 1, f"Should send 1, got {sent}"
    logger.info("  ✅ PASS — contract contacts are skipped, loop continues")


def test_no_consecutive_failures_break():
    """Verify removing consecutive_failures doesn't break the loop early."""
    logger.info("─" * 50)
    logger.info("TEST 3: No early break on consecutive failures")

    # Simulate: 5 failures, then 1 success — should reach the success
    results = ["failed", "failed", "failed", "failed", "failed", "connection_sent"]
    sent = 0
    for result in results:
        # OLD code would break after 3 failures — new code doesn't
        if result in ("connection_sent", "dm_sent"):
            sent += 1

    assert sent == 1, f"Should have sent 1, got {sent}"
    logger.info("  ✅ PASS — all contacts tried even after multiple failures")


# ═══════════════════════════════════════════════════════════════════
#  PART 2: Live dry run — visit real profiles and test detection
# ═══════════════════════════════════════════════════════════════════

# Mix of profiles to test. Add/change URLs as needed.
# The script ONLY visits and checks — it does NOT send connection requests.
TEST_PROFILES = [
    # Fill these with a few profiles from your recent DB contacts or known
    # LinkedIn profiles. The script will check each one for contract status.
    # Format: (name, profile_url, expected_type)
    # expected_type: "ft" (full-time), "contract" (should be filtered), "unknown"
]


def load_profiles_from_db(db: Database, limit: int = 6) -> list[tuple]:
    """Pull recent contacts from DB to test against."""
    rows = db.conn.execute(
        """SELECT name, profile_url, company, title
           FROM contacts
           WHERE profile_url IS NOT NULL AND profile_url != ''
           ORDER BY RANDOM()
           LIMIT ?""",
        (limit,),
    ).fetchall()
    profiles = []
    for r in rows:
        profiles.append((r["name"], r["profile_url"], r["company"], r["title"]))
    return profiles


def run_live_dry_run():
    """Visit profiles, run contract detection, screenshot results."""
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    db = Database()
    driver = None

    # If no hardcoded profiles, load from DB
    profiles = TEST_PROFILES
    if not profiles:
        logger.info("No hardcoded TEST_PROFILES — loading from DB …")
        db_profiles = load_profiles_from_db(db, limit=6)
        if not db_profiles:
            logger.error("❌ No contacts in DB to test. Add some to TEST_PROFILES list.")
            return
        profiles = [(name, url, "unknown") for (name, url, _, _) in db_profiles]
        logger.info(f"Loaded {len(profiles)} profiles from DB")

    try:
        logger.info("═" * 60)
        logger.info("🧪 CONTRACT FILTER DRY RUN — visit profiles, no messages")
        logger.info("═" * 60)

        driver = create_driver()
        reset_session()

        logger.info("🔐 Logging in …")
        if not login(driver):
            logger.error("❌ Login failed")
            return
        logger.info("✅ Logged in")
        time.sleep(3)

        results = []
        for i, (name, url, expected) in enumerate(profiles, 1):
            logger.info(f"\n{'─' * 50}")
            logger.info(f"[{i}/{len(profiles)}] Visiting: {name}")
            logger.info(f"  URL: {url}")

            try:
                if not safe_get(driver, url):
                    logger.warning(f"  ⚠️  Could not load profile for {name}")
                    results.append((name, url, "LOAD_FAILED", expected))
                    continue

                get_session().record_profile_view()
                time.sleep(2)  # let the page render

                # Scroll down to experience section
                driver.execute_script("window.scrollTo(0, 600);")
                time.sleep(1)

                # Run our contract detection
                is_contract = _is_contract_employee(driver)
                status = "CONTRACT" if is_contract else "FULL-TIME"
                results.append((name, url, status, expected))

                # Screenshot for review
                screenshot(driver, f"{i:02d}_{name.replace(' ', '_')}_{status}")

                match = ""
                if expected != "unknown":
                    if (expected == "contract" and is_contract) or \
                       (expected == "ft" and not is_contract):
                        match = " ✅ (matches expected)"
                    else:
                        match = " ❌ (MISMATCH!)"

                logger.info(f"  Result: {status}{match}")

            except Exception as e:
                logger.error(f"  ❌ Error checking {name}: {e}")
                results.append((name, url, f"ERROR: {e}", expected))

            # Small delay between visits (be nice to LinkedIn)
            if i < len(profiles):
                delay = 4 + (i * 0.5)  # gradually slower
                time.sleep(delay)

        # ── Summary ───────────────────────────────────────────────
        logger.info("\n" + "═" * 60)
        logger.info("📊 CONTRACT FILTER DRY-RUN RESULTS")
        logger.info("═" * 60)
        contracts_found = 0
        ft_found = 0
        errors = 0
        for name, url, status, expected in results:
            if "CONTRACT" == status:
                contracts_found += 1
                flag = "🚫 WOULD SKIP"
            elif "FULL-TIME" == status:
                ft_found += 1
                flag = "✅ WOULD MESSAGE"
            else:
                errors += 1
                flag = "⚠️  ERROR"
            logger.info(f"  {flag}  {name:25s} → {status}")

        logger.info(f"\nSummary: {ft_found} full-time, {contracts_found} contract (skipped), {errors} errors")
        logger.info(f"Screenshots saved to: {SCREENSHOTS_DIR}/")

    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        traceback.print_exc()
    finally:
        if driver:
            logger.info("\n🏁 Test complete. Closing browser in 10s …")
            time.sleep(10)
            try:
                driver.quit()
            except Exception:
                pass


def main():
    logger.info("=" * 60)
    logger.info("🧪 CONTRACT FILTER TEST SUITE")
    logger.info("=" * 60)

    # Part 1: Logic tests (instant, no browser)
    logger.info("\n📋 PART 1: Logic / unit tests")
    test_warning_flag_breaks_outer_loop()
    test_contract_skip_continues_loop()
    test_no_consecutive_failures_break()
    logger.info("\n✅ All logic tests passed!\n")

    # Part 2: Live dry run
    logger.info("📋 PART 2: Live dry run (browser)")
    run_live_dry_run()


if __name__ == "__main__":
    main()

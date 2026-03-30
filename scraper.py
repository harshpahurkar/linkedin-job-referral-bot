"""
Job scraper — searches LinkedIn Jobs and extracts listings via Selenium.
"""

import hashlib
import random
import re
import urllib.parse
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    TimeoutException,
    StaleElementReferenceException,
    WebDriverException,
)

from config import Config
from models import Job, Database
from utils import get_logger, human_delay
from antidetect import (
    get_session, is_session_safe, check_for_linkedin_warnings,
)

logger = get_logger("scraper")

# ── Company Blacklist (module-level so post_hunter.py can import it) ──
# Only block gig platforms, data labeling, freelance marketplaces,
# predatory training traps, and pure staffing/temp middlemen.
# Real IT companies (even body shops like Infosys/Cognizant/CGI) are kept —
# they hire FTEs and a referral there is still a real job.
_COMPANY_BLACKLIST_KEYWORDS = {
    # ── Data labeling / AI training gig platforms ────────────
    "alignerr", "labelbox", "outlier", "remotasks", "appen",
    "scale ai", "telus international", "telus digital",
    "dataannotation", "data annotation", "surge ai", "sama",
    "clickworker", "lionsbridge", "lionbridge",
    "welocalize", "transperfect", "defined.ai", "defined ai",
    "superhuman", "invisible technologies", "samasource",
    "cloudfactory", "mighty ai", "playment", "snorkel ai",
    "prolific", "toloka", "hive micro",
    "mercor", "peroptyx",

    # ── Freelance / contractor marketplace platforms ─────────
    "crossover", "toptal", "upwork", "fiverr", "freelancer.com",
    "guru.com", "turing", "andela", "revelo", "proxify",
    "arc.dev", "gun.io", "lemon.io", "braintrust",
    "working not working", "flexiple", "x-team", "micro1",
    "gigster", "codementor", "peopleperhour", "hubstaff talent",

    # ── Predatory training / contract-trap firms ─────────────
    "revature", "smoothstack",
    "fdm group", "fdm ",
    "talent path", "cogent infotech",
    "mthree", "wiley edge",
    "genspark", "htd talent",

    # ── Pure staffing / temp middlemen ───────────────────────
    "robert half", "randstad", "adecco", "manpower", "experis",
    "kelly services", "kelly science",
    "hays recruitment", "aerotek",
    "actalent", "insight global", "aston carter",
    "teksystems", "tek systems", "kforce",
    "collabera", "apex systems",
    "beacon hill staffing", "beacon hill",
    "mondo", "cybercoders", "jobot",
    "judge group", "mitchell martin",
    "motion recruitment", "yoh services", "yoh,",
    "mason frank", "nigel frank",
    "frank recruitment", "jefferson frank",
    "harvey nash", "tenth revolution",
    "altis hr", "appleone", "tds personnel",
    "staffworks", "express employment",
    "brooksource", "metasource",
    "calculated hire", "eight eleven",
    "robert walters", "creative circle",
    "vmr consultants", "mindtek",
    "akkodis", "modis",
    "pyramid consulting", "market street talent",
    "apc workforce", "zerochaos",
    "procom", "s.i. systems", "si systems",
    "tundra technical", "thompson trembley",

    # ── Generic keyword patterns (catch-all for agencies) ────
    "staffing agency", "recruiting agency", "recruitment agency",
    "talent solutions", "workforce solutions",
    "temp agency", "temporary staffing",
    "contract staffing", "placement agency",
}

# ── Constants ─────────────────────────────────────────────────────────
# LinkedIn uses .scaffold-layout__list-item for ALL 25 cards per page (even
# occluded ones).  .job-card-container only matches the ~7 that are rendered.
CARD_SEL = ".scaffold-layout__list-item"

EXP_LEVEL_MAP = {
    "internship": "1",
    "entry_level": "2",
    "associate": "3",
    "mid_senior": "4",
    "director": "5",
    "executive": "6",
}

REMOTE_MAP = {
    "on_site": "1",
    "remote": "2",
    "hybrid": "3",
}

# Junk text that LinkedIn appends to card titles
_TITLE_JUNK_PATTERNS = [
    r"\s*with verification.*",
    r"\s*with verified.*",
    r"\s*\u00b7\s*actively hiring.*",
    r"\s*-\s*actively hiring.*",
    r"\s*actively hiring.*",
]


def build_search_url(
    keyword: str,
    start: int = 0,
    location: str | None = None,
    remote_filter: list[str] | None = None,
    time_filter: str | None = None,
) -> str:
    """Build a LinkedIn Jobs search URL with all configured filters.

    Args:
        time_filter: LinkedIn f_TPR value, e.g. 'r3600' (1 h), 'r86400' (24 h).
                     Overrides Config.JOB_POSTED_WITHIN when provided.
    """
    params: dict[str, str] = {
        "keywords": keyword,
        "location": location or Config.JOB_LOCATION,
        "f_TPR": time_filter or getattr(Config, 'JOB_POSTED_WITHIN', 'r86400'),
    }

    if start > 0:
        params["start"] = str(start)

    # Experience level
    exp_codes = [
        EXP_LEVEL_MAP[lvl]
        for lvl in Config.EXPERIENCE_LEVEL
        if lvl in EXP_LEVEL_MAP
    ]
    if exp_codes:
        params["f_E"] = ",".join(exp_codes)

    # Remote filter (per-location override or global default)
    filters = remote_filter if remote_filter is not None else Config.REMOTE_FILTER
    remote_codes = [
        REMOTE_MAP[r] for r in filters if r in REMOTE_MAP
    ]
    if remote_codes:
        params["f_WT"] = ",".join(remote_codes)

    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


MAX_PAGES = 5  # up to 5 pages per keyword×location — scrape everything possible


# ── Cascading time windows ────────────────────────────────────────────
# Freshest jobs are the highest-value targets — someone who posted 1 h
# ago is far more likely to respond than a week-old listing.  We sweep
# narrowest window first, then progressively widen only if we haven't
# scraped enough jobs to fill the daily target.
_TIME_WINDOWS: list[tuple[str, str]] = [
    ("r3600",   "last 1 hour"),
    ("r7200",   "last 2 hours"),
    ("r14400",  "last 4 hours"),
    ("r28800",  "last 8 hours"),
    ("r57600",  "last 16 hours"),
    ("r86400",  "last 24 hours"),
    ("r172800", "last 48 hours"),
]


def scrape_jobs(driver: webdriver.Chrome, db: Database) -> list[Job]:
    """
    Scrape job listings for every configured keyword across all locations.
    Returns ALL jobs across all windows (used by dry-run mode).

    Uses a **cascading time-window** strategy so the freshest postings
    are discovered first:
        1 h → 2 h → 4 h → 8 h → 16 h → 24 h → 48 h
    Each window does the full keyword × location sweep.  Jobs already
    found in a tighter window are deduped automatically (by job_id).
    """
    all_jobs: list[Job] = []
    for batch in scrape_jobs_by_window(driver, db):
        all_jobs.extend(batch)
    return all_jobs


def scrape_jobs_by_window(driver: webdriver.Chrome, db: Database):
    """
    Generator: scrape one time window at a time and yield a filtered,
    ranked batch of new jobs after each window completes.

    This enables the interleaved pipeline — the caller can start
    messaging the yielded batch while the next window hasn't been
    scraped yet:

        for batch in scrape_jobs_by_window(driver, db):
            msgs += find_and_message_employees(driver, db, batch)
            if msgs >= daily_target:
                break  # don't bother scraping more windows

    Each yielded batch contains only jobs NEW to that window (no dupes
    from prior windows), already filtered and ranked.
    """
    all_new_jobs: list[Job] = []
    # Track which time window each job was discovered in (0 = freshest)
    job_window_map: dict[str, int] = {}

    # Multi-location search: each keyword × location combination
    search_locations = getattr(Config, 'JOB_SEARCH_LOCATIONS', None)
    if not search_locations:
        search_locations = [(Config.JOB_LOCATION, Config.REMOTE_FILTER)]

    search_combos = [
        (kw, loc, filters)
        for kw in Config.JOB_KEYWORDS
        for loc, filters in search_locations
    ]

    # ── Adaptive keyword tracking ────────────────────────────────
    # If a keyword yields 0 new jobs in N consecutive windows, skip
    # it for the rest of the run to save session time.
    _CONSEC_ZERO_THRESHOLD = 2  # skip after 2 consecutive zero-yield windows
    keyword_zero_streak: dict[str, int] = {kw: 0 for kw in Config.JOB_KEYWORDS}
    dead_keywords: set[str] = set()

    for window_idx, (time_code, window_label) in enumerate(_TIME_WINDOWS):
        logger.info("=" * 50)
        logger.info(
            f"⏱️  Time window {window_idx + 1}/{len(_TIME_WINDOWS)}: "
            f"{window_label}  (f_TPR={time_code})"
        )
        logger.info("=" * 50)

        # ── Anti-detection: check session is still safe ─────────
        if not is_session_safe():
            logger.critical("🛑 LinkedIn warning detected — aborting scrape!")
            break

        window_new = 0

        for keyword, search_location, remote_filters in search_combos:
            # ── Adaptive skip: keyword has been dead for too long ──
            if keyword in dead_keywords:
                continue

            logger.info(f"  🔍 Searching: {keyword} in {search_location}")
            keyword_new = 0
            keyword_dupes = 0
            empty_pages = 0  # consecutive pages with 0 cards

            for page_num in range(MAX_PAGES):
                offset = page_num * 25
                url = build_search_url(
                    keyword, start=offset, location=search_location,
                    remote_filter=remote_filters, time_filter=time_code,
                )
                try:
                    driver.get(url)
                except WebDriverException as exc:
                    logger.error(f"  Could not load page {page_num + 1}: {exc}")
                    break

                # Wait for job cards to actually render before checking anything.
                # LinkedIn's JS needs time to hydrate the page; a fixed 2-3 s
                # delay was too short and caused the bot to scan the DOM while
                # only the skeleton / reCAPTCHA iframe had loaded.
                try:
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, CARD_SEL))
                    )
                except TimeoutException:
                    pass  # page may genuinely have 0 results — handled below
                human_delay(3, 5)

                # ── Anti-detection: check for warnings after each page load
                get_session().record_search()
                warning, reason = check_for_linkedin_warnings(driver)
                if warning:
                    logger.critical(f"🛑 Warning on search page: {reason}")
                    break

                # Scroll this page's left panel to load all cards
                cards = _scroll_page_cards(driver)
                if not cards:
                    empty_pages += 1
                    if page_num == 0:
                        logger.warning(f"  No cards found for '{keyword}' in {search_location}")
                        _save_debug_snapshot(driver)
                    if empty_pages >= 2:
                        break  # two empty pages in a row → done
                    continue
                empty_pages = 0

                page_new = 0
                page_dupes = 0

                for card in cards:
                    # Scroll the card into view so LinkedIn renders its content
                    try:
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});", card
                        )
                        human_delay(0.15, 0.25)
                    except (StaleElementReferenceException, WebDriverException):
                        continue

                    try:
                        basics = _extract_card_basics(card)
                    except (StaleElementReferenceException, WebDriverException):
                        continue
                    if basics is None:
                        continue

                    job_id, title, company, location, job_url = basics

                    if db.job_exists(job_id):
                        page_dupes += 1
                        keyword_dupes += 1
                        continue

                    description = _click_and_get_description(driver, card)

                    job = Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        url=job_url,
                        description=description,
                    )
                    if db.insert_job(job):
                        all_new_jobs.append(job)
                        job_window_map[job.job_id] = window_idx
                        page_new += 1
                        keyword_new += 1

                logger.info(
                    f"  Page {page_num + 1}: {len(cards)} cards → "
                    f"{page_new} new, {page_dupes} dupes"
                )

            logger.info(
                f"  ✅ {keyword_new} new, {keyword_dupes} dupes for '{keyword}' in {search_location}"
            )
            window_new += keyword_new

            # ── Adaptive keyword tracking: update zero-yield streak ──
            if keyword_new == 0:
                keyword_zero_streak[keyword] = keyword_zero_streak.get(keyword, 0) + 1
                if keyword_zero_streak[keyword] >= _CONSEC_ZERO_THRESHOLD and keyword not in dead_keywords:
                    dead_keywords.add(keyword)
                    logger.info(
                        f"  💤 Skipping '{keyword}' for remaining windows "
                        f"({_CONSEC_ZERO_THRESHOLD} consecutive zero-yield windows)"
                    )
            else:
                keyword_zero_streak[keyword] = 0  # reset on any new finds

        logger.info(
            f"⏱️  Window \"{window_label}\" done: {window_new} new this window, "
            f"{len(all_new_jobs)} total so far"
        )

        # ── Filter & rank THIS window's new jobs, yield immediately ──
        if window_new > 0:
            window_jobs = [j for j in all_new_jobs if job_window_map.get(j.job_id) == window_idx]
            batch = _filter_and_rank(window_jobs, job_window_map, window_idx)
            if batch:
                logger.info(
                    f"📤 Yielding {len(batch)} jobs from \"{window_label}\" for outreach"
                )
                yield batch
        else:
            if all_new_jobs:
                logger.info("  ℹ️  No new jobs in this window (all dupes or empty)")

    logger.info(f"📦 Total jobs scraped across all windows & keywords: {len(all_new_jobs)}")


# ── Garbage title detection ───────────────────────────────────────────
# Some LinkedIn postings have broken/generic titles that are not real
# job listings (e.g. Evertz's "Thanks for visiting our Job Board").
_GARBAGE_TITLE_PHRASES = [
    "visit our job board", "review our open positions",
    "apply to the positions", "check out our careers",
    "see our open roles", "view all jobs",
    "click here to apply", "apply now",
]
_MIN_TITLE_LENGTH = 4
_MAX_TITLE_LENGTH = 120


def _is_garbage_title(title: str) -> bool:
    """Return True if a job title looks like a broken/generic posting."""
    t = title.strip()
    if len(t) < _MIN_TITLE_LENGTH or len(t) > _MAX_TITLE_LENGTH:
        return True
    t_lower = t.lower()
    return any(phrase in t_lower for phrase in _GARBAGE_TITLE_PHRASES)


# ── Seniority blacklist (used by filter) ──────────────────────────────
_SENIORITY_BLACKLIST = {
    "senior", "sr.", "sr ", "staff", "principal", "lead",
    "director", "vp", "vice president", "head of",
    "chief", "manager", "architect", "distinguished",
    "team lead", "tech lead", "engineering manager",
}


def _filter_and_rank(
    jobs: list[Job],
    job_window_map: dict[str, int],
    window_idx: int,
) -> list[Job]:
    """Filter out junk companies & senior roles, then rank by relevance.

    Called per-window so the interleaved pipeline gets a clean, ranked
    batch to message immediately.
    """
    if not jobs:
        return []

    # ── Hard-filter: drop garbage/broken titles ───────────────────
    pre_garbage = len(jobs)
    jobs = [job for job in jobs if not _is_garbage_title(job.title)]
    filtered_garbage = pre_garbage - len(jobs)
    if filtered_garbage:
        logger.info(
            f"🚫 Filtered out {filtered_garbage} garbage/broken job titles"
        )

    # ── Hard-filter: drop junk companies ──────────────────────────
    pre_company = len(jobs)
    jobs = [
        job for job in jobs
        if not any(blk in job.company.lower() for blk in _COMPANY_BLACKLIST_KEYWORDS)
    ]
    filtered_company = pre_company - len(jobs)
    if filtered_company:
        logger.info(
            f"🚫 Filtered out {filtered_company} junk companies "
            f"(staffing/gig/outsourcing/body shops)"
        )

    # ── Dedup multi-location postings (same company + title) ─────
    # Companies like BDO Canada post the same role in 8 cities.
    # Keep only the best-scoring location to free up batch slots.
    _loc_boost = (
        "toronto", "mississauga", "markham", "brampton",
        "scarborough", "north york", "etobicoke", "vaughan",
        "richmond hill", "oakville", "burlington", "hamilton",
        "gta", "greater toronto", "remote",
    )
    seen_roles: dict[tuple[str, str], Job] = {}
    deduped_locations = 0
    for job in jobs:
        key = (job.company.lower().strip(), job.title.lower().strip())
        if key in seen_roles:
            deduped_locations += 1
            # Prefer Toronto-area or remote locations over others
            loc = job.location.lower() if job.location else ""
            existing_loc = seen_roles[key].location.lower() if seen_roles[key].location else ""
            if any(s in loc for s in _loc_boost) and not any(s in existing_loc for s in _loc_boost):
                seen_roles[key] = job
        else:
            seen_roles[key] = job
    if deduped_locations:
        logger.info(
            f"🔗 Collapsed {deduped_locations} multi-location dupes "
            f"(same company + title)"
        )
    jobs = list(seen_roles.values())

    # ── Hard-filter: drop senior / lead / staff titles ────────────
    eligible: list[Job] = []
    filtered_senior = 0
    for job in jobs:
        t = job.title.lower()
        if any(term in t for term in _SENIORITY_BLACKLIST):
            filtered_senior += 1
        else:
            eligible.append(job)

    if filtered_senior:
        logger.info(
            f"🚫 Filtered out {filtered_senior} senior/lead/manager roles"
        )

    if not eligible:
        if jobs:
            logger.warning("⚠️  All jobs were senior-level — returning best matches anyway")
            eligible = jobs
        else:
            return []

    # ── Rank by relevance + freshness ──────────────────────────────
    num_windows = len(_TIME_WINDOWS)
    scored = [
        (
            _score_job_relevance(
                job,
                Config.JOB_KEYWORDS,
                job_window_map.get(job.job_id, num_windows - 1),
                num_windows,
            ),
            job,
        )
        for job in eligible
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    selected = [job for _, job in scored]

    logger.info(f"🏆 {len(selected)} jobs ranked by relevance + freshness:")
    _WINDOW_LABELS = {i: label for i, (_, label) in enumerate(_TIME_WINDOWS)}
    for i, (score, job) in enumerate(scored[:15], 1):
        w_idx = job_window_map.get(job.job_id, num_windows - 1)
        w_label = _WINDOW_LABELS.get(w_idx, "?")
        logger.info(
            f"  {i:>2}. [{score:+.1f}] ({w_label}) {job.title}  @  {job.company}"
            f"  ({job.location})"
        )
    if len(scored) > 15:
        logger.info(f"  … and {len(scored) - 15} more")

    return selected


# ── Internal helpers ──────────────────────────────────────────────────


def _score_job_relevance(
    job: Job,
    keywords: list[str],
    window_idx: int = 0,
    num_windows: int = 7,
) -> float:
    """Score how relevant a job title is to the user's target roles.

    Higher = better match.  Combines keyword relevance with a freshness
    bonus so that recently-posted jobs rank higher when relevance is
    similar.

    Freshness bonus scale (7 windows):
        Window 0 (1 hr)  → +4.8
        Window 1 (2 hr)  → +4.0
        Window 2 (4 hr)  → +3.2
        Window 3 (8 hr)  → +2.4
        Window 4 (16 hr) → +1.6
        Window 5 (24 hr) → +0.8
        Window 6 (48 hr) → +0.0

    This is strong enough to re-order similarly-scored jobs by freshness,
    but a full keyword match (+10) still dominates over freshness alone.
    """
    title_lower = job.title.lower()
    score = 0.0

    for kw in keywords:
        kw_lower = kw.lower()
        # Full keyword match in title → big boost
        if kw_lower in title_lower:
            score += 10.0
        else:
            # Partial word matching (e.g. "Software" from "Software Engineer")
            kw_words = kw_lower.split()
            matches = sum(1 for w in kw_words if w in title_lower)
            if kw_words:
                score += (matches / len(kw_words)) * 7.0

    # Bonus for core tech terms that signal relevant roles
    _BONUS_TERMS = {
        "software", "developer", "engineer", "full-stack", "fullstack",
        "full stack", "backend", "frontend", "front-end", "back-end",
        "devops", "cloud", "platform", "sre", "python", "java", "react",
        "node", "aws", "azure", "microservices",
    }
    for term in _BONUS_TERMS:
        if term in title_lower:
            score += 1.0

    # Bonus for junior/entry-level signals
    _JUNIOR_TERMS = {"junior", "jr.", "jr ", "entry", "associate", "new grad", "graduate"}
    for term in _JUNIOR_TERMS:
        if term in title_lower:
            score += 3.0
            break

    # ── Freshness bonus: fresher jobs get a meaningful boost ──────
    # 0.8 points per window closer to the freshest (window 0).
    # Max bonus is (num_windows - 1) * 0.8 = 4.8 for the 1-hour window.
    freshness_bonus = (num_windows - 1 - window_idx) * 0.8
    score += freshness_bonus

    # ── Toronto / GTA location boost ─────────────────────────────
    # We search Canada-wide in one pass, but prefer jobs near Toronto.
    loc_lower = job.location.lower() if job.location else ""
    _TORONTO_SIGNALS = (
        "toronto", "mississauga", "markham", "brampton",
        "scarborough", "north york", "etobicoke", "vaughan",
        "richmond hill", "oakville", "burlington", "hamilton",
        "gta", "greater toronto",
    )
    if any(sig in loc_lower for sig in _TORONTO_SIGNALS):
        score += 3.0

    return round(score, 1)


# JavaScript that finds the real scrollable ancestor of the job list.
# LinkedIn obfuscates class names, so we walk up from the first card
# until we find the element whose overflow-y is 'auto' or 'scroll'.
_JS_SCROLL_DOWN = """
    const card = document.querySelector(arguments[0]);
    if (!card) { window.scrollBy(0, 800); return -1; }
    let el = card.parentElement;
    while (el && el !== document.body) {
        const s = getComputedStyle(el);
        if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight) {
            el.scrollTop += 800;
            return el.scrollTop;
        }
        el = el.parentElement;
    }
    window.scrollBy(0, 800);
    return window.scrollY;
"""

_JS_SCROLL_TOP = """
    const card = document.querySelector(arguments[0]);
    if (!card) { window.scrollTo(0,0); return; }
    let el = card.parentElement;
    while (el && el !== document.body) {
        const s = getComputedStyle(el);
        if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight) {
            el.scrollTop = 0;
            return;
        }
        el = el.parentElement;
    }
    window.scrollTo(0, 0);
"""


def _scroll_page_cards(driver: webdriver.Chrome) -> list:
    """
    Scroll the real scrollable parent of the job list to render all ~25 cards.
    Uses JS to walk up from the first card and find the overflow container.

    LinkedIn inserts survey / promotional "breaker" elements in the middle
    of the card list.  These are NOT job cards and don't match CARD_SEL,
    so a naive "stable card count → stop" heuristic exits too early and
    misses all the real job cards BELOW the breaker.

    Strategy: keep scrolling until we see the **pagination bar**
    (the 1, 2, 3, 4, 5 page buttons at the bottom) which is a reliable
    signal that we've reached the end of the list.  Fall back to a
    generous stable-count threshold so we don't get stuck forever if
    pagination is absent (e.g. very few results).
    """
    # Selectors for LinkedIn's pagination bar at the bottom of the job list
    _PAGINATION_SELS = (
        "ul.artdeco-pagination__pages",         # standard pagination
        ".artdeco-pagination",                   # outer wrapper
        ".jobs-search-pagination",               # alternate class
        "[aria-label='Page navigation']",        # aria fallback
    )

    prev_count = 0
    stable = 0

    for scroll_round in range(30):  # generous cap (was 15)
        try:
            driver.execute_script(_JS_SCROLL_DOWN, CARD_SEL)
        except WebDriverException:
            break
        human_delay(0.3, 0.5)

        # ── Check if we've scrolled past everything and can see
        #    the pagination buttons — that means the full list has loaded.
        try:
            pagination_visible = driver.execute_script("""
                const sels = arguments[0];
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) return true;
                }
                return false;
            """, list(_PAGINATION_SELS))
        except WebDriverException:
            pagination_visible = False

        if pagination_visible:
            # Give LinkedIn one more moment to render any final cards
            human_delay(0.3, 0.5)
            break

        # ── Stable-count fallback (for searches with few results
        #    where pagination is absent).  Use a HIGH threshold (7)
        #    so breaker elements don't fool us into stopping early.
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, CARD_SEL))
        except WebDriverException:
            break

        if count == prev_count:
            stable += 1
            if stable >= 7:
                break
        else:
            stable = 0
        prev_count = count

    # Scroll back to top so clicking cards works from the start
    try:
        driver.execute_script(_JS_SCROLL_TOP, CARD_SEL)
        human_delay(0.3, 0.4)
    except WebDriverException:
        pass

    try:
        return driver.find_elements(By.CSS_SELECTOR, CARD_SEL)
    except WebDriverException:
        return []



def _clean_title(raw: str) -> str:
    """Strip verification badges, duplicate lines, and junk from a title."""
    # Take only the first meaningful line
    title = raw.split("\n")[0].strip()

    # Remove junk suffixes
    for pattern in _TITLE_JUNK_PATTERNS:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE).strip()

    # Remove trailing punctuation artifacts
    title = title.rstrip("·–—- ").strip()
    return title


def _extract_card_basics(card) -> tuple[str, str, str, str, str] | None:
    """
    Pull (job_id, title, company, location, url) from a card element.
    Returns None if the card can't be parsed.

    Uses a JS-first approach to avoid Selenium's comma-separated CSS
    selector pitfall (it returns the first DOM match across ALL selectors,
    which can mix up title/company when LinkedIn re-orders elements).
    Falls back to ordered Selenium queries if JS fails.
    """
    try:
        # ── Primary: JS extraction (single call, no stale-element risk) ──
        data = card.parent.execute_script("""
            const card = arguments[0];

            // Helper — get clean visible text (innerText avoids the
            // textContent duplication bug with nested spans)
            function txt(el) {
                return (el.innerText || el.textContent || '').trim().split('\\n')[0].trim();
            }

            // Title + URL — always in the main <a> link
            const titleLink = card.querySelector(
                'a.job-card-list__title--link') ||
                card.querySelector('a.job-card-container__link') ||
                card.querySelector('.job-card-list__title a') ||
                card.querySelector('a[href*="/jobs/view/"]');
            if (!titleLink) return null;
            const title = txt(titleLink);
            const href  = (titleLink.href || '').split('?')[0];
            if (!title) return null;

            // Company — try specific selectors in priority order
            let company = '';
            const companySelectors = [
                '.job-card-container__primary-description',
                '.job-card-container__company-name',
                '.artdeco-entity-lockup__subtitle',
            ];
            for (const sel of companySelectors) {
                const el = card.querySelector(sel);
                if (el) {
                    company = txt(el);
                    if (company) break;
                }
            }

            // Location — try specific selectors in priority order
            let location = '';
            const locSelectors = [
                '.job-card-container__metadata-wrapper',
                '.job-card-container__metadata-item',
                '.artdeco-entity-lockup__caption',
            ];
            for (const sel of locSelectors) {
                const el = card.querySelector(sel);
                if (el) {
                    location = txt(el);
                    if (location) break;
                }
            }

            return {title, href, company: company || 'Unknown', location};
        """, card)

        if not data:
            return None

        title = _clean_title(data["title"])
        job_url = data["href"]
        company = data["company"]
        location = data["location"]

        if not title:
            return None

        # ── Sanity check: swap if company looks like a role ──────────
        _ROLE_WORDS = {"engineer", "developer", "manager", "analyst",
                       "designer", "scientist", "intern", "lead",
                       "architect", "devops", "sre", "qa", "sdet",
                       "recruiter", "specialist"}
        _ROLE_PATTERN = re.compile(
            r'\b(?:' + '|'.join(re.escape(w) for w in _ROLE_WORDS) + r')\b'
        )
        company_lower = company.lower()
        if _ROLE_PATTERN.search(company_lower) and company != "Unknown":
            logger.warning(
                f"  ⚠️  Company looks like a role — skipping card "
                f"(title='{title}', company='{company}')"
            )
            return None

        job_id = hashlib.md5(f"{title}|{company}|{job_url}".encode()).hexdigest()[:16]
        logger.debug(f"  📋 Card: title='{title}' | company='{company}' | loc='{location}'")
        return job_id, title, company, location, job_url

    except (StaleElementReferenceException, NoSuchElementException):
        return None
    except Exception as exc:
        logger.debug(f"Card parse error: {exc}")
        return None


def _click_and_get_description(driver: webdriver.Chrome, card) -> str:
    """Click a job card so the detail panel loads, then extract the description."""
    try:
        # Scroll the card into view first so it's rendered and clickable
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", card
        )
        human_delay(0.2, 0.3)

        try:
            card.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", card)
        except (StaleElementReferenceException, WebDriverException):
            return ""

        human_delay(0.8, 1.2)  # quick pause for panel to load

        # Try several possible selectors for the description panel
        _DESC_SELECTORS = [
            "#job-details",
            ".jobs-description-content__text",
            ".jobs-description__content",
            ".jobs-box__html-content",
        ]

        for sel in _DESC_SELECTORS:
            try:
                el = WebDriverWait(driver, 2).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                text = el.text.strip()
                if text and len(text) > 50:
                    return text[:2000]  # cap at 2000 chars
            except TimeoutException:
                continue

        return ""
    except (WebDriverException, Exception) as exc:
        logger.debug(f"Could not read description: {exc}")
        return ""


def _save_debug_snapshot(driver: webdriver.Chrome):
    """Persist a screenshot for later inspection.

    Only saves a screenshot — page source is NOT written to disk
    because it may contain CSRF tokens, session fragments, or
    other sensitive data that shouldn't sit in plaintext.
    """
    try:
        debug_path = Path(__file__).parent / "data" / "debug_page.png"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(debug_path))
        logger.info(f"  📸 Debug screenshot saved to {debug_path}")
    except Exception as exc:
        logger.debug(f"Could not save debug snapshot: {exc}")

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

logger = get_logger("scraper")

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


def build_search_url(keyword: str, start: int = 0) -> str:
    """Build a LinkedIn Jobs search URL with all configured filters."""
    params: dict[str, str] = {
        "keywords": keyword,
        "location": Config.JOB_LOCATION,
        "f_TPR": "r86400",  # posted in last 24 hours
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

    # Remote filter
    remote_codes = [
        REMOTE_MAP[r] for r in Config.REMOTE_FILTER if r in REMOTE_MAP
    ]
    if remote_codes:
        params["f_WT"] = ",".join(remote_codes)

    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


MAX_PAGES = 3  # up to 3 pages per keyword for broader coverage


def scrape_jobs(driver: webdriver.Chrome, db: Database) -> list[Job]:
    """
    Scrape job listings for every configured keyword.
    Uses URL-based pagination (start=0, 25, 50 …) to walk through pages.
    On each page, scrolls the left panel to load all visible cards.
    """
    all_new_jobs: list[Job] = []

    for keyword in Config.JOB_KEYWORDS:
        logger.info(f"🔍 Searching: {keyword}")
        keyword_new = 0
        keyword_dupes = 0
        empty_pages = 0  # consecutive pages with 0 cards

        for page_num in range(MAX_PAGES):
            offset = page_num * 25
            url = build_search_url(keyword, start=offset)
            try:
                driver.get(url)
            except WebDriverException as exc:
                logger.error(f"  Could not load page {page_num + 1}: {exc}")
                break
            human_delay(2, 3)

            # Scroll this page's left panel to load all cards
            cards = _scroll_page_cards(driver)
            if not cards:
                empty_pages += 1
                if page_num == 0:
                    logger.warning(f"  No cards found for '{keyword}'")
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
                    page_new += 1
                    keyword_new += 1

            logger.info(
                f"  Page {page_num + 1}: {len(cards)} cards → "
                f"{page_new} new, {page_dupes} dupes"
            )

        logger.info(
            f"  ✅ {keyword_new} new, {keyword_dupes} dupes for '{keyword}'"
        )

    logger.info(f"📦 Total jobs scraped across all keywords: {len(all_new_jobs)}")

    if not all_new_jobs:
        return all_new_jobs

    # ── Hard-filter: drop senior / lead / staff titles ────────────
    _SENIORITY_BLACKLIST = {
        "senior", "sr.", "sr ", "staff", "principal", "lead",
        "director", "vp", "vice president", "head of",
        "chief", "manager", "architect", "distinguished",
        "team lead", "tech lead", "engineering manager",
    }
    eligible: list[Job] = []
    filtered_senior = 0
    for job in all_new_jobs:
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
        logger.warning("⚠️  All jobs were senior-level — returning best matches anyway")
        eligible = all_new_jobs  # fallback so we don't return nothing

    # ── Rank by relevance ────────────────────────────────────────
    # Many companies yield 0 reachable contacts, so we need ~2×
    # the number of companies to actually hit the daily target.
    # Formula: (daily_target / per_company) * 2, but never fewer
    # than all eligible jobs (no point throwing away good ones).
    scored = [
        (_score_job_relevance(job, Config.JOB_KEYWORDS), job)
        for job in eligible
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    needed_companies = max(
        20,
        (Config.MAX_MESSAGES_PER_DAY // Config.MAX_MESSAGES_PER_COMPANY) * 2,
    )
    top_n = min(needed_companies, len(scored))
    selected = [job for _, job in scored[:top_n]]

    logger.info(f"🏆 Top {top_n} jobs selected by relevance:")
    for i, (score, job) in enumerate(scored[:top_n], 1):
        logger.info(
            f"  {i:>2}. [{score:+.1f}] {job.title}  @  {job.company}"
            f"  ({job.location})"
        )

    if len(scored) > top_n:
        skipped = len(scored) - top_n
        logger.info(f"  … {skipped} lower-relevance jobs skipped")

    # Shuffle the selected set so outreach order isn't predictable
    random.shuffle(selected)
    return selected


# ── Internal helpers ──────────────────────────────────────────────────


def _score_job_relevance(job: Job, keywords: list[str]) -> float:
    """Score how relevant a job title is to the user's target roles.

    Higher = better match.  Penalises senior/staff roles when the user
    targets entry-level / associate positions.
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
    """
    prev_count = 0
    stable = 0

    for _ in range(15):
        try:
            driver.execute_script(_JS_SCROLL_DOWN, CARD_SEL)
        except WebDriverException:
            break
        human_delay(0.3, 0.5)

        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, CARD_SEL))
        except WebDriverException:
            break

        if count == prev_count:
            stable += 1
            if stable >= 3:
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
        company_lower = company.lower()
        if any(w in company_lower for w in _ROLE_WORDS) and company != "Unknown":
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
    """Persist page source + screenshot for later inspection."""
    try:
        debug_path = Path(__file__).parent / "data" / "debug_page.html"
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(driver.page_source, encoding="utf-8")
        logger.info(f"  💾 Debug page saved to {debug_path}")
        driver.save_screenshot(str(debug_path.with_suffix(".png")))
        logger.info(f"  📸 Screenshot saved to {debug_path.with_suffix('.png')}")
    except Exception as exc:
        logger.debug(f"Could not save debug snapshot: {exc}")

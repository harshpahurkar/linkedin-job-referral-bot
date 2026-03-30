"""
Post Hunter — finds "hiring" posts on LinkedIn and engages with the poster.

Strategy:
  1. Search LinkedIn posts for hiring-related keywords
  2. Extract each post's text, poster info, and company
  3. Score each post for legitimacy (detect bait vs real hiring)
  4. For high-scoring posts: DM the poster or leave a comment
  5. Track everything in the DB to avoid duplicate engagement

Safety measures:
  - Legitimacy scoring system (poster title, specificity, bait signals)
  - Separate weekly budget (doesn't eat into connection budget)
  - Company blacklist reuse (same junk filter as job scraper)
  - Poster role verification (must be recruiter/HM/engineer, not coach)
  - Bait detection (engagement farming, generic "react if…" posts)
  - Rate limiting + human-like delays between engagements
"""

import hashlib
import random
import re
import urllib.parse

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains

from config import Config
from models import Contact, Database
from messenger import _send_connection_with_note, _is_canadian_location
from utils import get_logger, human_delay, long_delay, simulate_random_mouse_movement
from antidetect import (
    get_session, is_session_safe, check_for_linkedin_warnings,
    smart_delay, should_take_break, simulate_natural_break,
)

logger = get_logger("post_hunter")


# ═══════════════════════════════════════════════════════════════════════
#  LEGITIMACY SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════
# Each post gets a score 0–100. Only posts above POST_HUNT_MIN_SCORE
# (default 40) get engaged with.

# ── Poster title signals ──────────────────────────────────────────────
# People who can actually hire or refer you.
_LEGIT_POSTER_TITLES = {
    # Recruiters (they literally hire people)
    "recruiter": 25, "recruiting": 25, "talent acquisition": 25,
    "talent partner": 20, "sourcer": 20, "hiring": 20,
    "people operations": 15, "human resource": 15, "hr manager": 15,
    # Hiring managers / eng leadership
    "engineering manager": 25, "director of engineering": 25,
    "vp of engineering": 25, "vp engineering": 25,
    "head of engineering": 25, "cto": 20,
    "tech lead": 20, "team lead": 20,
    # Engineers who post about open roles on their team
    "software engineer": 15, "senior engineer": 18,
    "staff engineer": 18, "principal engineer": 18,
    "developer": 12, "architect": 15,
    "sre": 12, "devops": 12, "platform engineer": 12,
}

# People whose "hiring" posts are almost always bait / self-promo.
_BAIT_POSTER_TITLES = [
    "career coach", "resume writer", "job search",
    "linkedin coach", "linkedin trainer", "linkedin expert",
    "career mentor", "career consultant", "career strategist",
    "personal brand", "content creator", "influencer",
    "motivational speaker", "life coach", "mindset",
    "career advisor", "outplacement", "job coach",
    "founder at hire", "ceo at hire",  # "HireMe" type startups
]

# ── Post text signals ─────────────────────────────────────────────────
# Specific details = real hiring post. Generic fluff = bait.

# POSITIVE signals: specifics about the actual role
_LEGIT_POST_SIGNALS = [
    (r"\b(hiring|open role|open position|looking for)\b", 5),
    (r"\b(my team|our team|our company|we are hiring|we're hiring)\b", 10),
    (r"\b(apply|application|link in comment|link below|dm me)\b", 5),
    (r"\b(remote|hybrid|on-?site|in-?office)\b", 5),      # work type = real
    (r"\b(junior|mid[- ]?level|entry[- ]?level|new grad|recent grad|associate)\b", 8),  # our target seniority
    (r"\b(full-?stack|backend|frontend|devops|cloud|sre|data|ml|ai)\b", 8),
    (r"\b(salary|compensation|tc|total comp|\$\d+k)\b", 10),  # $ = very real
    (r"\b(referral|refer)\b", 5),
    (r"\b(python|java|react|node|aws|kubernetes|docker|typescript|go|rust)\b", 3),
    # Canada geo boost (we ONLY want Canadian posts)
    (r"\b(canada|canadian)\b", 12),
    (r"\b(toronto|vancouver|montreal|ottawa|calgary|edmonton|winnipeg)\b", 12),
    (r"\b(ontario|british columbia|quebec|alberta|gta|bc|ab|on)\b", 8),
    (r"\b(waterloo|kitchener|mississauga|brampton|hamilton|london,? on)\b", 8),
    (r"\bremote.{0,15}canada\b", 15),  # "remote in Canada" = perfect
]

# ── Canada geo keywords for hard filtering ─────────────────────────
# Post must contain at least ONE of these to be considered.
# This ensures we don't waste connections on US/India/EU posts.
_CANADA_GEO_KEYWORDS = [
    "canada", "canadian",
    # Provinces
    "ontario", "british columbia", "quebec", "alberta",
    "manitoba", "saskatchewan", "nova scotia", "new brunswick",
    # Major cities
    "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "edmonton", "winnipeg", "halifax", "victoria",
    # GTA / tech hubs
    "gta", "waterloo", "kitchener", "mississauga", "brampton",
    "hamilton", "markham", "richmond hill", "burnaby", "surrey",
    # Abbreviations common in LinkedIn posts
    " on ", " bc ", " ab ", " qc ",
    # Remote-friendly patterns (matched loosely)
    "remote canada", "remote - canada", "remote (canada",
    "#canada", "#toronto", "#vancouver", "#montreal",
]

# ── Seniority filter ──────────────────────────────────────────────────
# User is junior/mid — skip posts explicitly hiring for senior+.
# We check both the post text AND the role being advertised.
_SENIOR_ROLE_KEYWORDS = [
    "senior", "staff", "principal", "lead",
    "director", "head of", "vp ", "vp of",
    "architect", "distinguished", "fellow",
    "manager",   # engineering manager posts = not for us
    "sr.", "sr ",
]


def _is_senior_role(post_text: str, poster_title: str) -> bool:
    """
    Return True if the post is advertising a senior+ role.

    We look for senior keywords in the HIRING context of the post text,
    not in the poster's own title (a Senior Eng posting a junior role is fine).
    Only triggers if the senior keyword appears near hiring language.
    """
    text_lower = post_text.lower()

    # Quick patterns: "hiring senior", "looking for a staff", "open role: principal"
    for kw in _SENIOR_ROLE_KEYWORDS:
        # Check if the senior keyword appears in a hiring context
        if re.search(
            rf"\b(?:hiring|looking for|open (?:role|position)|seeking|need)\b"
            rf".{{0,30}}"
            rf"{re.escape(kw)}",
            text_lower,
        ):
            return True
        # Also check "Senior Software Engineer" pattern (role title)
        if re.search(
            rf"\b{re.escape(kw)}\s*(?:software|full.?stack|backend|frontend|devops|cloud|data|platform|sre|ml)",
            text_lower,
        ):
            return True

    return False


# NEGATIVE signals: engagement bait, self-promo, or scammy
_BAIT_SIGNALS = [
    # ── Engagement farming ─────────────────────────────────────
    (r"\breact if\b", -30),
    (r"\blike if you('re| are) (looking|open|searching)\b", -30),
    (r"\bcomment .{0,20}(yes|interested|me|i'm in)\b", -25),
    (r"\b(drop your resume|send me your cv)\b", -15),
    (r"\btag (someone|a friend|people)\b", -20),
    (r"\brepost (this|to help)\b", -15),
    (r"\b(follow me|follow for)\b", -20),
    (r"\b(motivat|inspir|grind|hustle|mindset)\b", -15),
    (r"\b(100 (people|devs|engineers))\b", -15),
    (r"\bfree (course|bootcamp|workshop|webinar)\b", -20),
    (r"\b(coaching|mentoring program|1-on-1)\b", -15),
    (r"\b(secret|hack|trick|nobody tells)\b", -15),
    (r"\b#opentowork\b", -5),
    (r"\b(laid off|let go|fired)\b", -10),

    # ── Spam / scam / multi-country junk ──────────────────────
    (r"\bnigeria\b", -40),
    (r"\b(global opportunities|multiple countries|worldwide hiring)\b", -35),
    (r"\b(urgent hiring alert|urgent requirement)\b", -30),
    (r"\b(c2c|corp.to.corp|corp to corp|contract.to.contract)\b", -35),
    (r"\b(staffing agency|staffing firm|placement agency)\b", -25),
    (r"\b(visa sponsor|h1b|h-1b|opt|cpt|ead|gc holder)\b", -20),
    (r"\b(work permit|sponsorship available)\b", -10),
    (r"\b(batch hiring|mass hiring|bulk hiring|100\+ openings)\b", -25),
    (r"\b(dm for details|dm me for|inbox me|whatsapp)\b", -20),
    (r"\b(pay rate|hourly rate|\$\d+\/hr)\b", -15),  # staffing C2C posts
    (r"\b(immediate joiner|immediate start|spot offer)\b", -20),
    (r"\b(hot requirement|hot job|urgent need)\b", -25),
    (r"\b(walkin|walk-in|walk in interview)\b", -30),
    (r"\b(naukri|indeed\.com|monster\.com|glassdoor)\b", -15),
    (r"\b(india|bangalore|hyderabad|chennai|mumbai|pune|noida|gurugram)\b", -30),
    (r"\b(pakistan|dubai|uae|saudi|qatar|bahrain|oman|kuwait)\b", -30),
    (r"\b(south africa|kenya|ghana|egypt|morocco)\b", -25),
    (r"\b(philippines|singapore|malaysia)\b", -15),
    # Self-promo / not actually hiring
    (r"\b(subscribe|join my newsletter|my new book)\b", -20),
    (r"\b(check out my|my latest (post|article|video))\b", -15),
    (r"\b(congratulations to me|i('m| am) (excited|thrilled) to announce)\b", -10),

    # ── Staffing farm / body-shop patterns ────────────────────
    (r"#w2\b", -35),                  # W2 contract = staffing body-shop
    (r"\bw-?2\b.*\b(contract|only)\b", -30),
    (r"#c2c\b", -35),                 # corp-to-corp = staffing shell
    (r"\b\d+\s*openings?\b", -15),    # "5 Openings" = bulk staffing
    # Pipe-separated country lists = multi-country spam
    (r"(?:usa?|us|uk|india|uae|australia)\s*[|/]\s*(?:usa?|uk|india|canada|uae)", -25),
    (r"canada\s*[|/]\s*(?:usa?|us|uk|india|uae|australia)", -25),
    # International / global spam patterns (softer than before — score, don't reject)
    (r"\binternational.*(?:hiring|recruitment|opportunity)\b", -15),
    (r"\b(global|worldwide)\s+(?:it\s+)?hiring\b", -20),

    # ══════════════════════════════════════════════════════════
    #  NEW RED FLAGS (from internet research — Reddit, r/recruitinghell,
    #  r/linkedin, r/resumes, r/ExperiencedDevs, r/Scams)
    # ══════════════════════════════════════════════════════════

    # ── "No experience" / zero-barrier scam posts ─────────────
    (r"\bno\s+experience\s+(required|needed|necessary)\b", -20),
    (r"\bno\s+qualifications?\s+(required|needed|necessary)\b", -15),
    (r"\bzero\s+experience\b", -20),
    (r"\bno\s+degree\s+(required|needed)\b", -10),

    # ── Urgency / pressure language ───────────────────────────
    (r"\burgent(ly)?\s+(hiring|requirement|need|opening)\b", -15),
    (r"\b(immediate\s+start|immediate\s+joining|immediate\s+joiner)\b", -15),
    (r"\b(apply\s+now|apply\s+immediately|apply\s+today|apply\s+asap)\b", -10),
    (r"\b(hurry|limited\s+spots?|don'?t\s+miss|act\s+fast)\b", -10),
    (r"\b(spot\s+offer|instant\s+offer|same\s+day\s+offer)\b", -20),

    # ── "Kindly" = offshore scam marker (r/Scams consensus) ──
    (r"\bkindly\b", -10),

    # ── "DM me" / resume harvesting ───────────────────────────
    (r"\b(dm\s+me|inbox\s+me|message\s+me\s+directly)\b", -10),
    (r"\b(drop\s+your\s+(resume|cv)|send\s+your\s+(resume|cv))\b", -10),
    (r"\b(share\s+your\s+(resume|cv)|forward\s+your\s+(resume|cv))\b", -10),

    # ── "Fast advancement" / MLM / too good to be true ────────
    (r"\b(fast\s+advancement|rapid\s+growth\s+opportunity)\b", -10),
    (r"\b(unlimited\s+earning|unlimited\s+income|unlimited\s+potential)\b", -25),
    (r"\b(earn\s+from\s+home|earn\s+\$?\d+[kK]?\s*(/|per)\s*(day|week|month))\b", -25),
    (r"\b(make\s+money\s+(from|at)\s+home|work\s+from\s+home\s+earn)\b", -25),
    (r"\b(entry\s+level\s+marketing|entry\s+level\s+sales\s+rep)\b", -25),
    (r"\b(be\s+your\s+own\s+boss|financial\s+freedom|passive\s+income)\b", -25),
    (r"\b(multi[- ]?level|network\s+marketing|mlm)\b", -30),
    (r"\b(residual\s+income|downline|upline)\b", -30),

    # ── Off-platform push (WhatsApp/Telegram/WeChat) ──────────
    (r"\b(whatsapp|telegram|wechat|signal\s+app)\b", -20),
    (r"\b(contact\s+(me|us)\s+on\s+whatsapp)\b", -25),
    (r"\b(join\s+(our|my)\s+(telegram|whatsapp)\s+(group|channel))\b", -25),

    # ── Training-company-as-employer scam ─────────────────────
    (r"\b(training\s+provided|we\s+provide\s+training)\b", -10),
    (r"\b(training\s+program|paid\s+training|free\s+training)\b", -10),
    (r"\b(bootcamp\s+hiring|bootcamp\s+graduate)\b", -5),

    # ── Copy-paste "feel good" engagement bait (r/linkedin) ──
    (r"\bi\s+hired\s+a\s+candidate\s+with\s+zero\s+experience\b", -30),
    (r"\b(agree\s*\?|thoughts\s*\?|am\s+i\s+right\s*\?)\b", -10),
    (r"\b(share\s+if\s+you\s+agree|repost\s+if)\b", -20),
    (r"\b(controversial\s+take|unpopular\s+opinion|hot\s+take)\b", -10),

    # ── Job-SEEKER posts (not hiring — wrong target) ─────────
    # These are people looking for work, not posting about open roles
    (r"\bi'?m\s+(looking|searching|seeking)\s+(for\s+)?(a\s+)?(new\s+)?(job|role|position|opportunity)", -40),
    (r"\b(open\s+to\s+work|actively\s+(looking|seeking|searching))\b", -35),
    (r"\b(laid\s+off|recently\s+let\s+go|just\s+got\s+laid\s+off)\b", -30),
    (r"\b(i\s+was\s+(let\s+go|terminated|laid\s+off))\b", -30),
    (r"\b(seeking\s+new\s+opportunities?|looking\s+for\s+my\s+next)\b", -35),
    (r"\b(i\s+need\s+a\s+(job|referral|role))\b", -30),
    (r"\b(would\s+appreciate\s+your\s+(help|support|referral))\b", -25),
    (r"\b(please\s+(refer|connect)\s+me)\b", -30),
    (r"\b(any\s+(leads|openings|referrals)\s*\?)\b", -20),
    (r"\b(my\s+resume|my\s+cv|my\s+portfolio)\b", -15),
    (r"\b(#opentowork|#lookingforjob|#jobseeking|#jobhunt)\b", -30),
]

# ── Company red flags (added to scoring, not just blacklist) ──────
_SUSPECT_COMPANY_SIGNALS = [
    "stealth",        # can be legit startup, slight penalty
    "staffing",
    "consulting",     # often staffing agencies
    "recruitment",
    "manpower",
    "hr solutions",
    "talent solutions",
    "global solutions",
    "infotech",       # Indian staffing body-shops
    "technologies llc",  # common C2C shell company pattern
    "inc.",           # not a penalty on its own
]


def _is_canadian_role(post_text: str, poster_title: str, company: str) -> bool:
    """
    Canada filter: returns True if the post mentions Canada at all.

    Multi-country spam is penalised by the scoring engine (bait signals
    for pipe-list patterns, India/Nigeria mentions, etc.) — NOT hard-
    rejected here.  Some legit Canadian roles mention other countries
    too (e.g. "remote Canada or US").
    """
    blob = f"{post_text} {poster_title} {company}".lower()

    # Must mention Canada / a Canadian city or province
    return any(kw in blob for kw in _CANADA_GEO_KEYWORDS)


def _score_poster_title(title: str) -> int:
    """Score the poster's job title — are they someone who actually hires?"""
    title_lower = title.lower()

    # Check for bait poster first (coaches, influencers, etc.)
    for bait in _BAIT_POSTER_TITLES:
        if bait in title_lower:
            return -30  # heavy penalty

    # Check for legit hiring titles
    best = 0
    for keyword, points in _LEGIT_POSTER_TITLES.items():
        if keyword in title_lower:
            best = max(best, points)

    return best


def _score_post_text(text: str) -> int:
    """Score the post text for legitimacy signals vs bait signals."""
    text_lower = text.lower()
    score = 0

    for pattern, points in _LEGIT_POST_SIGNALS:
        if re.search(pattern, text_lower):
            score += points

    for pattern, penalty in _BAIT_SIGNALS:
        if re.search(pattern, text_lower):
            score += penalty  # penalty is already negative

    # Length heuristic: extremely short posts (<50 chars) are usually bait
    if len(text.strip()) < 50:
        score -= 10
    # Very long posts (>1500 chars) are often thought-leadership, not hiring
    if len(text.strip()) > 1500:
        score -= 5

    # ── Emoji density: engagement-farming posts are loaded with emoji ──
    # Count emoji using the Unicode emoji ranges
    emoji_count = len(re.findall(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        r"\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
        r"\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
        r"\U0000FE00-\U0000FE0F\U0000200D]",
        text,
    ))
    if emoji_count > 10:
        score -= 20
    elif emoji_count > 5:
        score -= 10

    return score


def _is_blacklisted_company(company: str) -> bool:
    """Reuse the scraper's company blacklist to filter junk companies."""
    if not company:
        return False
    # Import the blacklist from scraper (it's a module-level set inside a function,
    # so we duplicate the critical entries here for independence)
    from scraper import _COMPANY_BLACKLIST_KEYWORDS
    company_lower = company.lower()
    return any(blk in company_lower for blk in _COMPANY_BLACKLIST_KEYWORDS)


def _is_empty_company(company: str) -> bool:
    """Treat blank or 'confidential' as effectively no company."""
    if not company or not company.strip():
        return True
    return company.strip().lower() in ("confidential", "confidential company")


def _score_company(company: str) -> int:
    """Penalise suspicious company names (staffing agencies, 'Confidential', etc.)."""
    if _is_empty_company(company):
        return -15  # no company / hidden = significant red flag
    c = company.lower()
    penalty = 0
    for sus in _SUSPECT_COMPANY_SIGNALS:
        if sus in c:
            penalty -= 10
    return penalty


def score_post(poster_title: str, post_text: str, company: str) -> int:
    """
    Calculate overall legitimacy score (0–100 scale, clamped).

    Components:
      - Poster title score (are they a recruiter/HM/engineer?)
      - Post text score (specific role details vs generic bait)
      - Company check (blacklisted = instant zero, suspect = penalty)
    """
    if _is_blacklisted_company(company):
        return 0

    title_score = _score_poster_title(poster_title)
    text_score = _score_post_text(post_text)
    company_score = _score_company(company)

    total = title_score + text_score + company_score

    # ── Combo: recruiter-type title but no company = fake recruiter farm ──
    # Real recruiters / TA people ALWAYS have a company listed.
    if _is_empty_company(company):
        t = poster_title.lower()
        if any(kw in t for kw in (
            "talent acquisition", "recruiter", "recruiting",
            "staffing", "human resource", "hr manager",
            "career advisor", "placement", "hiring",
        )):
            total -= 30

    # Clamp to 0–100
    return max(0, min(100, total))


# ═══════════════════════════════════════════════════════════════════════
#  POST SEARCH + EXTRACTION
# ═══════════════════════════════════════════════════════════════════════

def _build_post_search_url(keyword: str, date_filter: str = "past-week") -> str:
    """Build a LinkedIn content/post search URL.

    Appends 'Canada' to the keyword if it doesn't already contain a
    Canadian geo term — this nudges LinkedIn's search to prioritise
    posts from / about Canadian roles.

    Args:
        keyword:      Search keyword.
        date_filter:  LinkedIn datePosted value — "past-24h" or "past-week".
    """
    kw_lower = keyword.lower()
    has_geo = any(
        g in kw_lower
        for g in ("canada", "toronto", "vancouver", "montreal", "ottawa")
    )
    search_kw = keyword if has_geo else f"{keyword} Canada"

    params = {
        "keywords": search_kw,
        "origin": "GLOBAL_SEARCH_HEADER",
        "datePosted": date_filter,   # "past-24h" first, then "past-week" fallback
        "sortBy": "date_posted",     # newest first
    }
    return "https://www.linkedin.com/search/results/content/?" + urllib.parse.urlencode(params)


def _click_all_more_buttons(driver: webdriver.Chrome) -> None:
    """Click every '…more' / 'see more' button on the page.

    LinkedIn truncates long posts behind a "…more" link.  Expanding them
    reveals full text with locations, tech stacks, and role details that
    are critical for scoring, Canada filtering, and seniority detection.

    Uses JS to find and click all such buttons in one shot — fast and
    doesn't require scrolling back to each one.
    """
    try:
        clicked = driver.execute_script("""
            let count = 0;
            // LinkedIn uses various text for the expand button
            const buttons = document.querySelectorAll(
                'button, span[role="button"], a'
            );
            for (const btn of buttons) {
                const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (txt === '…more' || txt === '...more' || txt === 'see more'
                    || txt === '\u2026more' || txt === 'more') {
                    // Only click small inline buttons, not nav links
                    const r = btn.getBoundingClientRect();
                    if (r.width > 0 && r.width < 200 && r.height > 0 && r.height < 60) {
                        btn.click();
                        count++;
                    }
                }
            }
            return count;
        """)
        if clicked:
            # Brief wait for DOM to re-render expanded text
            import time
            time.sleep(0.4)
    except Exception:
        pass  # non-critical, extraction still works with truncated text


def _scroll_feed_for_posts(driver: webdriver.Chrome) -> None:
    """Scroll LinkedIn content-search results to load more posts.

    Mimics human reading behaviour — matches the anti-detection patterns
    used in scraper.py (_scroll_page_cards) and messenger.py
    (_simulate_profile_reading):
      * Detects whether LinkedIn uses a nested scroll container (2026 DOM)
        or the main window, and scrolls the right thing.
      * Alternates between ActionChains wheel-scroll events (real
        pointer input) and JS scrollBy (keyboard-shortcut equivalent).
      * Random distances, mouse movements, and occasional "reading"
        pauses so the pattern never looks robotic.
      * Checks post count (profile-link count) instead of page height
        for more reliable lazy-load detection.
    """
    # ── Detect a nested scroll container (LinkedIn 2026 may use one) ──
    has_container = driver.execute_script("""
        const links = document.querySelectorAll('a[href*="/in/"]');
        for (const link of links) {
            let el = link;
            for (let i = 0; i < 12; i++) {
                el = el.parentElement;
                if (!el || el === document.body) break;
                const s = getComputedStyle(el);
                if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                    && el.scrollHeight > el.clientHeight + 100) {
                    return true;
                }
            }
        }
        return false;
    """)

    scroll_rounds = random.randint(10, 16)
    prev_post_count = driver.execute_script(
        "return document.querySelectorAll('a[href*=\"/in/\"]').length"
    )
    stale_rounds = 0

    logger.debug(
        f"    Scroll: {scroll_rounds} rounds planned, "
        f"container={has_container}, initial_links={prev_post_count}"
    )

    for _si in range(scroll_rounds):
        scroll_px = random.randint(400, 850)

        if has_container:
            # Scroll the nested container (same approach as scraper.py)
            driver.execute_script("""
                const links = document.querySelectorAll('a[href*="/in/"]');
                for (const link of links) {
                    let el = link;
                    for (let j = 0; j < 12; j++) {
                        el = el.parentElement;
                        if (!el || el === document.body) break;
                        const s = getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                            && el.scrollHeight > el.clientHeight + 100) {
                            el.scrollBy(0, arguments[0]);
                            return;
                        }
                    }
                }
                window.scrollBy(0, arguments[0]);
            """, scroll_px)
        else:
            # Alternate between ActionChains wheel-scroll (real pointer
            # input) and JS scrollBy — real humans use both.
            if random.random() < 0.55:
                try:
                    ActionChains(driver).scroll_by_amount(0, scroll_px).perform()
                except Exception:
                    driver.execute_script(f"window.scrollBy(0, {scroll_px});")
            else:
                driver.execute_script(f"window.scrollBy(0, {scroll_px});")

        # ── Human behaviour during scrolling ─────────────────────
        # Mouse wiggle every 2-3 scrolls (like eyes scanning posts)
        if _si % random.randint(2, 3) == 0:
            simulate_random_mouse_movement(driver)

        # Wait for lazy-loaded content to render
        human_delay(1.2, 2.0)

        # 20% chance of a longer pause (actually reading a post)
        if random.random() < 0.20:
            simulate_random_mouse_movement(driver)
            human_delay(1.5, 3.0)

        # ── Check for new content (profile-link count, not height) ──
        new_count = driver.execute_script(
            "return document.querySelectorAll('a[href*=\"/in/\"]').length"
        )
        if new_count == prev_post_count:
            stale_rounds += 1
            if stale_rounds >= 3:
                break  # no new content after 3 rounds
        else:
            stale_rounds = 0
            prev_post_count = new_count

    logger.debug(f"    Scroll complete: {prev_post_count} profile links visible")

    # Scroll back to top so extraction gets all cards in DOM order
    driver.execute_script("window.scrollTo(0, 0);")
    human_delay(0.5, 1.0)


def _click_show_more_or_next(driver: webdriver.Chrome) -> bool:
    """Try to click LinkedIn's 'Show more results' / pagination button.

    LinkedIn content search sometimes has a 'Show more results' button at
    the bottom, or numbered page links.  Returns True if we found and
    clicked one, False otherwise.
    """
    try:
        clicked = driver.execute_script(r"""
            // ── 1. "Show more results" / "See more results" button ──
            const buttons = document.querySelectorAll('button, span[role="button"]');
            for (const btn of buttons) {
                const txt = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                if (/show more|see more results|load more/.test(txt)) {
                    const r = btn.getBoundingClientRect();
                    if (r.width > 30 && r.height > 15 && r.bottom > 0) {
                        btn.scrollIntoView({behavior: 'smooth', block: 'center'});
                        btn.click();
                        return 'show_more';
                    }
                }
            }

            // ── 2. "Next" pagination link ──
            const links = document.querySelectorAll('a, button');
            for (const el of links) {
                const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                if (txt === 'next' || aria.includes('next page') || aria === 'next') {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 10 && r.bottom > 0) {
                        el.scrollIntoView({behavior: 'smooth', block: 'center'});
                        el.click();
                        return 'next';
                    }
                }
            }

            // ── 3. Numbered page 2 link ──
            for (const el of links) {
                const txt = (el.innerText || el.textContent || '').trim();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                if (txt === '2' && (aria.includes('page') || el.closest('nav, [role="navigation"]'))) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 10 && r.height > 10) {
                        el.scrollIntoView({behavior: 'smooth', block: 'center'});
                        el.click();
                        return 'page2';
                    }
                }
            }

            return null;
        """)

        if clicked:
            logger.debug(f"    📄 Clicked pagination: {clicked}")
            human_delay(2.0, 3.5)  # wait for page to load
            return True

    except Exception as e:
        logger.debug(f"    Pagination click failed: {e}")

    return False


def _simulate_search_reading(driver: webdriver.Chrome) -> None:
    """Brief human pause on a search results page before scrolling.

    Simulates reading the first few results — mirrors the
    _simulate_profile_reading() pattern from messenger.py.
    """
    simulate_random_mouse_movement(driver)
    human_delay(1.0, 2.0)
    # Small scroll like reading the first result
    driver.execute_script(f"window.scrollBy(0, {random.randint(150, 350)});")
    human_delay(0.5, 1.5)
    simulate_random_mouse_movement(driver)


def _extract_posts_from_page(driver: webdriver.Chrome) -> list[dict]:
    """
    Extract post data from a LinkedIn content search results page.

    LinkedIn 2026 uses hashed/obfuscated CSS classes so we can't rely on
    class names.  Instead we use a **structural** approach:
      1. Find all ``a[href*="/in/"]`` profile links (reliable anchors).
      2. Walk up the DOM to find the post-card container (DIV with ≥4
         children whose innerText starts with "Feed post").
      3. Parse the card's innerText which follows a predictable pattern::

             Feed post{Name} {Badge?} {Degree+}{Name} • {Degree+}
             {Title at Company}{Time} • Follow{PostBody}

    Returns list of dicts with: poster_name, poster_title, poster_url,
    company, post_text, post_url, reactions.
    """
    posts = driver.execute_script(r"""
        const results = [];
        const seenHrefs = new Set();

        document.querySelectorAll('a[href*="/in/"]').forEach(link => {
            try {
                const href = link.href;
                if (!href || !href.includes('/in/')) return;
                const cleanUrl = href.split('?')[0].replace(/\/$/, '');
                if (seenHrefs.has(cleanUrl)) return;

                // ── Walk up to find the post-card container ──
                let card = link;
                let found = false;
                for (let i = 0; i < 8; i++) {
                    card = card.parentElement;
                    if (!card) break;
                    if (card.tagName === 'DIV' && card.children.length >= 4) {
                        const t = (card.innerText || '');
                        if (t.trimStart().startsWith('Feed post')) {
                            const r = card.getBoundingClientRect();
                            if (r.height >= 80 && r.height <= 4000) {
                                found = true;
                                break;
                            }
                        }
                    }
                }
                if (!found) return;

                seenHrefs.add(cleanUrl);
                const fullText = card.innerText.trim();

                // Strip "Feed post" prefix (9 chars)
                const stripped = fullText.substring(9);

                // ── Poster name: text before badge/connection-degree ──
                // Pattern: "Name [Premium/Verified Profile] Degree+"
                let posterName = '';
                const nm = stripped.match(
                    /^(.+?)\s+(?:Premium Profile\s+|Verified Profile\s+)?\d+(?:st|nd|rd|th)\+/
                );
                if (nm) posterName = nm[1].trim();

                // Fallback: derive name from URL slug
                if (!posterName) {
                    const slug = cleanUrl.match(/\/in\/([^/]+)/);
                    if (slug) {
                        posterName = slug[1]
                            .replace(/-/g, ' ')
                            .replace(/\d+$/, '')
                            .trim()
                            .split(' ')
                            .map(w => w.charAt(0).toUpperCase() + w.slice(1))
                            .join(' ');
                    }
                }

                // ── First bullet separator  ( • ) ──
                const bIdx = stripped.indexOf(' \u2022 ');
                if (bIdx === -1) return;
                const afterBullet = stripped.substring(bIdx + 3);

                // Strip leading connection-degree prefix ("3rd+", "2nd", …)
                const cMatch = afterBullet.match(/^\d+(?:st|nd|rd|th)\+?\s*/);
                const afterConn = cMatch
                    ? afterBullet.substring(cMatch[0].length)
                    : afterBullet;

                // ── Split subtitle from post body at "• Follow" ──
                const fRe = /[\u2022\u00B7]\s*Follow(?:ing)?/;
                const fMatch = afterConn.match(fRe);
                if (!fMatch) return;

                let subtitle = afterConn.substring(0, fMatch.index).trim();
                const postBody = afterConn
                    .substring(fMatch.index + fMatch[0].length)
                    .trim();

                // Strip trailing time from subtitle
                // ("…Partners3m" → "…Partners")
                subtitle = subtitle
                    .replace(/\d+[mhdwy]\w{0,2}$/, '')
                    .replace(/Just now$/i, '')
                    .replace(/Edited$/i, '')
                    .trim();

                if (posterName && postBody.length > 15) {
                    results.push({
                        posterName,
                        posterUrl: cleanUrl,
                        subtitle,
                        postText: postBody.substring(0, 2000),
                        postUrl: '',
                        reactions: 0
                    });
                }
            } catch(e) { /* skip malformed cards */ }
        });

        return results;
    """) or []

    # ── Parse subtitle into poster_title + company ────────────────
    parsed = []
    for p in posts:
        subtitle = p.get("subtitle", "")
        poster_title = ""
        company = ""
        # LinkedIn format: "Title at Company", "Title @ Company", or
        # "Title | Company"
        if " at " in subtitle:
            parts = subtitle.split(" at ", 1)
            poster_title = parts[0].strip()
            company = parts[1].strip().split("\n")[0].strip()
        elif " @ " in subtitle:
            parts = subtitle.split(" @ ", 1)
            poster_title = parts[0].strip()
            company = parts[1].strip().split("\n")[0].strip()
        elif " | " in subtitle:
            parts = subtitle.split(" | ", 1)
            poster_title = parts[0].strip()
            company = parts[1].strip().split("\n")[0].strip()
        elif "\n" in subtitle:
            lines = [l.strip() for l in subtitle.split("\n") if l.strip()]
            poster_title = lines[0] if lines else ""
            company = lines[1] if len(lines) > 1 else ""
        else:
            poster_title = subtitle

        parsed.append({
            "poster_name": p["posterName"],
            "poster_title": poster_title,
            "poster_url": p["posterUrl"],
            "company": company,
            "post_text": p["postText"],
            "post_url": p["postUrl"],
            "reactions": p["reactions"],
        })

    return parsed


# ═══════════════════════════════════════════════════════════════════════
#  ENGAGEMENT ACTIONS
# ═══════════════════════════════════════════════════════════════════════

# DM templates for hiring post posters — shorter, more casual than
# referral templates because the poster already signaled they're hiring.
_POST_DM_TEMPLATES = [
    "Hi {poster_name}, saw your post about the {role_hint} role"
    " at {company}. I've been building production systems with"
    " {tech_hint} and this looks like a great fit. Would love to"
    " connect and learn more! {your_name}",

    "Hi {poster_name}, your post about the {role_hint} position"
    " at {company} caught my eye \u2014 I've got hands-on experience"
    " with {tech_hint} and have been shipping production code."
    " Would love to chat! {your_name}",

    "Hi {poster_name}, noticed you're hiring at {company}. I've"
    " been working with {tech_hint} building full-stack apps and"
    " automation tools. The {role_hint} role sounds like a strong"
    " fit — would love to connect! {your_name}",

    "Hi {poster_name}, saw your post about the {role_hint} opening"
    " at {company}! I'm a software dev experienced in {tech_hint},"
    " and this seems right up my alley. Would really appreciate a"
    " chance to connect! {your_name}",
]

# Possible comment responses (less intrusive than DM, good for engagement)
_POST_COMMENT_TEMPLATES = [
    "Really interested in this! I've been working with {tech_hint}"
    " and this role sounds like a great fit. Just sent you a connection"
    " request 🙏",

    "This is great to see! I have hands-on experience with {tech_hint}"
    " — would love to learn more about this role. Connecting!",
]


def _clean_role(raw: str) -> str:
    """Clean up an extracted role name for use in a DM."""
    role = raw.strip()
    # Strip leading filler adjectives that aren't part of the role name
    role = re.sub(
        r'^\s*(?:talented|experienced|skilled|passionate|motivated|'
        r'dedicated|exceptional|amazing|great|awesome|rockstar|ninja|'
        r'new|additional|multiple|several|remote|hybrid|the|a|an|'
        r'our|my|your|for|as)\s+',
        '', role, flags=re.IGNORECASE,
    ).strip()
    # De-pluralise common role suffixes (Engineers -> Engineer)
    for plural in ('engineers', 'developers', 'analysts', 'architects',
                   'designers', 'specialists', 'scientists', 'programmers',
                   'consultants', 'coordinators', 'administrators',
                   'technicians'):
        if role.lower().endswith(plural):
            role = role[:-1]
            break
    role = role.strip().title()
    role = re.sub(r'\s+', ' ', role).strip()
    return role if len(role) > 2 else "engineering"


def _extract_role_hint(post_text: str) -> str:
    """Extract the ACTUAL role being advertised from the post text.

    Analyses hiring context in the post to find the specific job title,
    instead of matching against a small hardcoded list.  Falls back
    gracefully when no specific role can be identified.

    Examples:
      "Linxon is hiring a BIM Engineer in Canada"  -> "Bim Engineer"
      "We have an open Cloud Engineer position"     -> "Cloud Engineer"
      "Looking for a Full-Stack Developer"          -> "Full-Stack Developer"
      "We're growing our team!"                     -> "engineering"
    """
    # Common endings of job titles
    _SUFFIX = (
        r"(?:engineers?|developers?|architects?|analysts?|designers?|"
        r"specialists?|coordinators?|scientists?|administrators?|"
        r"consultants?|technicians?|programmers?|interns?|"
        r"associates?|devops|sre|dbas?|sdets?|qa)"
    )

    # ── 1. "hiring / looking for [a/an] {Role}" ──────────────────
    m = re.search(
        rf"\b(?:hiring|looking\s+for|seeking|need|recruiting)"
        rf"\s+(?:a\s+|an\s+)?"
        rf"(?:(?:senior|junior|mid[- ]?level|staff|principal|lead)\s+)?"
        rf"((?:\w+[\s/&-]*){{0,4}}{_SUFFIX})\b",
        post_text, re.IGNORECASE,
    )
    if m:
        return _clean_role(m.group(1))

    # ── 2. "open role/position [for/:] {Role}" ───────────────────
    m = re.search(
        rf"\b(?:open\s+(?:role|position)|opening)"
        rf"\s*(?:for|as|:)?\s*(?:a\s+|an\s+)?"
        rf"((?:\w+[\s/&-]*){{0,4}}{_SUFFIX})\b",
        post_text, re.IGNORECASE,
    )
    if m:
        return _clean_role(m.group(1))

    # ── 3. "{Role} position/opening/role/opportunity" ─────────────
    m = re.search(
        rf"\b((?:\w+[\s/&-]*){{1,4}}{_SUFFIX})"
        rf"\s+(?:position|opening|role|opportunity)\b",
        post_text, re.IGNORECASE,
    )
    if m:
        return _clean_role(m.group(1))

    # ── 4. Broad domain-specific patterns ─────────────────────────
    m = re.search(
        r"\b((?:full[- ]?stack|back[\s-]?end|front[\s-]?end|software|"
        r"platform|cloud|data|devops|infrastructure|security|automation|"
        r"systems?|site\s+reliability|qa|machine\s+learning|ml|ai|"
        r"bi[m]?|etl|network)\s+(?:engineers?|developers?|architects?|"
        r"analysts?|scientists?))\b",
        post_text, re.IGNORECASE,
    )
    if m:
        return _clean_role(m.group(1))

    # ── 5. Catch-all: any "X Engineer/Developer" ──────────────────
    m = re.search(
        r"\b(\w+\s+(?:engineers?|developers?))\b",
        post_text, re.IGNORECASE,
    )
    if m:
        word = m.group(1).split()[0].lower()
        if word not in ("the", "a", "an", "our", "my", "your", "as",
                        "and", "or", "no", "any", "all"):
            return _clean_role(m.group(1))

    return "engineering"  # intentionally vague — better than wrong


def _extract_tech_hint(post_text: str) -> str:
    """Extract mentioned technologies from a hiring post."""
    text_lower = post_text.lower()

    tech_map = {
        "python": "Python", "java": "Java", "javascript": "JavaScript",
        "typescript": "TypeScript", "react": "React", "node.js": "Node.js",
        "node": "Node.js", "spring boot": "Spring Boot", "spring": "Spring",
        "aws": "AWS", "azure": "Azure", "docker": "Docker",
        "kubernetes": "Kubernetes", "terraform": "Terraform",
        "postgresql": "PostgreSQL", "mongodb": "MongoDB",
        "go ": "Go", "golang": "Go", "rust": "Rust",
        "django": "Django", "flask": "Flask", "angular": "Angular",
        "vue": "Vue", "next.js": "Next.js",
    }

    found = []
    seen = set()
    for keyword, display in tech_map.items():
        if keyword in text_lower and display not in seen:
            found.append(display)
            seen.add(display)

    if not found:
        return random.choice([
            "Java, Python, and cloud tools",
            "Python, React, and AWS",
            "Java, Spring Boot, and microservices",
        ])

    if len(found) == 1:
        return found[0]
    if len(found) == 2:
        return f"{found[0]} and {found[1]}"
    return f"{found[0]}, {found[1]}, and {found[2]}"


def _scrape_profile_info(driver: webdriver.Chrome) -> dict:
    """Scrape name + location from the currently-loaded LinkedIn profile.

    Returns {"name": str, "first_name": str, "location": str}.
    """
    info = driver.execute_script(r"""
        const result = {name: '', location: ''};

        // ── Name: the h1 inside the profile top-card ──
        const h1 = document.querySelector('h1');
        if (h1) {
            result.name = (h1.innerText || h1.textContent || '').trim();
        }

        // ── Location: the span with geo text below the headline ──
        // LinkedIn 2026 has it in a span inside a div below the h1,
        // but class names are hashed.  We find it structurally:
        //   - Must be BELOW the h1 vertically (not the name itself)
        //   - Must contain a comma ("City, Province, Country")
        //   - Must NOT overlap with the scraped name
        const nameText = result.name.toLowerCase();
        const nameWords = nameText.split(/[\s,]+/).filter(w => w.length > 2);
        const h1Rect = h1 ? h1.getBoundingClientRect() : {bottom: 100};

        const spans = document.querySelectorAll('span');
        for (const sp of spans) {
            const t = (sp.innerText || sp.textContent || '').trim();
            if (!t || t.length > 60 || t.length < 3) continue;
            const r = sp.getBoundingClientRect();
            // Must be BELOW the h1 name element and within the top card
            if (r.top < h1Rect.bottom || r.top > 500) continue;
            // Location strings usually look like "Toronto, Ontario, Canada"
            if (/,\s/.test(t) && /[A-Z]/.test(t)) {
                const tLow = t.toLowerCase();
                // Reject if text overlaps with the name (catches "Susan Chesa, C.E.T")
                const matchCount = nameWords.filter(w => tLow.includes(w)).length;
                if (nameWords.length > 0 && matchCount >= 2) continue;
                // Reject non-location text
                if (/ at /.test(t) || / \| /.test(t)) continue;
                // Reject mutual connections / follower badge text
                if (/\b(mutual|connections?|followers?|other)\b/i.test(t)) continue;
                // Reject professional credentials / suffixes (person names not locations)
                if (/\b(CPA|CMA|CFA|MBA|PhD|PMP|P\.?Eng|C\.?E\.?T|CISSP|CISM|CSM|ScrumMaster|RN|MD|JD|LLB|CA)\b/i.test(t)) continue;
                // Reject dates: month names or year numbers
                if (/\b(january|february|march|april|may|june|july|august|september|october|november|december)\b/i.test(t)) continue;
                if (/\b(19|20)\d{2}\b/.test(t)) continue;
                // Reject recommendation/endorsement text
                if (/\b(reported|directly|managed|supervised|recommended|endorsed|hired|worked with)\b/i.test(t)) continue;
                // Reject text that is clearly a sentence (has verbs)
                if (/\b(is|was|are|were|has|have|had|can|could|will|would|should)\b/i.test(t)) continue;
                // Positive validation: real locations usually have 2-4 comma-separated parts
                // and each part is 2-30 chars. Names with creds fail this.
                const parts = t.split(/,\s*/);
                if (parts.length < 2 || parts.length > 5) continue;
                const partsOk = parts.every(p => p.trim().length >= 2 && p.trim().length <= 35);
                if (!partsOk) continue;
                result.location = t;
                break;
            }
        }
        return result;
    """) or {}

    name = (info.get("name") or "").strip()
    # Clean artifacts: LinkedIn sometimes appends verification badges
    name = re.sub(r"\s*(Verified|Premium|He/Him|She/Her|They/Them|\(.*?\)).*$", "", name).strip()
    first_name = name.split()[0] if name else ""
    location = (info.get("location") or "").strip()

    return {"name": name, "first_name": first_name, "location": location}


def _scrape_profile_signals(driver: webdriver.Chrome) -> dict:
    """Scrape fake-profile indicators from the currently-loaded profile.

    Returns dict with:
      - connections: int  (0 if can't read)
      - has_photo: bool
      - headline_len: int
      - has_about: bool
      - experience_count: int
    """
    return driver.execute_script(r"""
        const sig = {
            connections: 0,
            has_photo: false,
            headline_len: 0,
            has_about: false,
            experience_count: 0
        };

        // ── 1. Connection / follower count ──
        // LinkedIn shows "500+ connections" or "123 connections" or "1K followers"
        const allSpans = document.querySelectorAll('span');
        for (const sp of allSpans) {
            const t = (sp.innerText || sp.textContent || '').trim().toLowerCase();
            // "500+ connections" or "123 connections" or "456 followers"
            const m = t.match(/(\d[\d,+kK]*)[\s+]*(connections?|followers?)/i);
            if (m) {
                let numStr = m[1].replace(/[,+]/g, '');
                if (/k/i.test(numStr)) {
                    sig.connections = Math.max(sig.connections,
                        parseFloat(numStr) * 1000);
                } else {
                    sig.connections = Math.max(sig.connections,
                        parseInt(numStr, 10) || 0);
                }
            }
        }

        // ── 2. Profile photo ──
        // Look for an img inside the profile header area (top 400px)
        const imgs = document.querySelectorAll('img');
        for (const img of imgs) {
            const r = img.getBoundingClientRect();
            if (r.top >= 0 && r.top < 400 && r.width >= 80 && r.height >= 80) {
                const src = (img.src || '').toLowerCase();
                // LinkedIn ghost/default avatar has specific patterns
                if (!src.includes('ghost') && !src.includes('default')
                    && !src.includes('placeholder') && src.length > 20) {
                    sig.has_photo = true;
                    break;
                }
            }
        }

        // ── 3. Headline length ──
        // The headline is typically in a div right below the h1 name
        const h1 = document.querySelector('h1');
        if (h1) {
            let el = h1.parentElement;
            if (el) {
                const siblings = el.parentElement ? el.parentElement.children : [];
                for (const sib of siblings) {
                    if (sib !== el && sib.tagName === 'DIV') {
                        const txt = (sib.innerText || '').trim();
                        if (txt.length > 5 && txt.length < 300) {
                            sig.headline_len = txt.length;
                            break;
                        }
                    }
                }
            }
        }

        // ── 4. About section ──
        // Look for a section with "About" heading
        const sections = document.querySelectorAll('section');
        for (const sec of sections) {
            const heading = sec.querySelector('h2, [id*="about"]');
            if (heading) {
                const ht = (heading.innerText || '').trim().toLowerCase();
                if (ht === 'about' || ht.includes('about')) {
                    const aboutText = sec.innerText || '';
                    if (aboutText.length > 50) {
                        sig.has_about = true;
                    }
                    break;
                }
            }
        }

        // ── 5. Experience entries count ──
        for (const sec of sections) {
            const heading = sec.querySelector('h2');
            if (heading) {
                const ht = (heading.innerText || '').trim().toLowerCase();
                if (ht === 'experience' || ht.includes('experience')) {
                    // Count list items or distinct company entries
                    const items = sec.querySelectorAll('li, [data-view-name]');
                    sig.experience_count = items.length;
                    break;
                }
            }
        }

        return sig;
    """) or {}


def _is_suspicious_profile(signals: dict) -> tuple[bool, str]:
    """Evaluate profile signals and decide if the profile looks fake.

    Returns (is_suspicious, reason_string).

    Key heuristics (based on LinkedIn fake-profile research):
      - Very low connection count (< 50) = newly created / bot account
      - No profile photo = likely fake / throwaway
      - No about section + no experience = skeleton profile
      - Extremely short headline = generic / incomplete
    """
    reasons = []
    score = 0

    connections = signals.get("connections", 0)
    has_photo = signals.get("has_photo", False)
    headline_len = signals.get("headline_len", 0)
    has_about = signals.get("has_about", False)
    experience_count = signals.get("experience_count", 0)

    # ── Connection count: only penalise if we positively found a low number ──
    if connections < 50 and connections > 0:
        score += 2
        reasons.append(f"low connections ({connections})")
    # connections == 0 → scraper couldn't read it → inconclusive, no penalty

    if not has_photo:
        score += 2
        reasons.append("no profile photo")

    # ── Headline: only penalise if we FOUND a very short one ──
    # headline_len == 0 means the scraper couldn't locate the element (common
    # with LinkedIn's obfuscated DOM), NOT that the headline is empty.
    if 0 < headline_len < 10:
        score += 1
        reasons.append(f"very short headline ({headline_len} chars)")

    # ── About + Experience: only flag genuinely skeleton profiles ──
    if not has_about and experience_count == 0:
        score += 2
        reasons.append("no about + no experience")
    # "no about" alone is too noisy — scraper often misses the section

    # Threshold: 2+ = suspicious  (catches no-photo bots, low-connection
    # fakes, and skeleton profiles while ignoring scraper failures)
    is_suspicious = score >= 2
    return is_suspicious, ", ".join(reasons) if reasons else "clean"


def _engage_poster(
    driver: webdriver.Chrome,
    poster_url: str,
    poster_name: str,
    poster_title: str,
    company: str,
    message: str,
) -> str:
    """
    Navigate to the poster's profile, verify they're Canadian,
    check for fake-profile signals, scrape their real name, then
    hand off to messenger.py's _send_connection_with_note.

    Returns 'connection_sent', 'dm_sent', 'skipped_not_canada',
    'skipped_fake', or 'failed'.
    """
    try:
        driver.get(poster_url)
        human_delay(1.5, 2.5)

        # ── Anti-detection: record view & check for warnings ─────
        get_session().record_profile_view()
        warning, reason = check_for_linkedin_warnings(driver)
        if warning:
            logger.critical(f"🛑 Warning while viewing poster profile: {reason}")
            return "failed"
    except Exception as e:
        logger.debug(f"  Could not load profile {poster_url}: {e}")
        return "failed"

    # ── Scrape real name + location from the profile page ─────────
    profile = _scrape_profile_info(driver)

    real_name = profile["name"] or poster_name
    first_name = profile["first_name"] or (poster_name.split()[0] if poster_name else "there")
    location = profile["location"]

    if real_name != poster_name:
        logger.info(f"    👤 Real name: {real_name} (was '{poster_name}' from URL)")

    # ── Canada location gate: skip non-Canadian posters ───────────
    if location:
        if _is_canadian_location(location):
            logger.info(f"    📍 Location: {location} ✅ (Canada)")
        else:
            logger.info(f"    📍 Location: {location} ❌ (not Canada — skipping)")
            return "skipped_not_canada"
    else:
        logger.debug(f"    📍 Could not determine location for {real_name}")
        # No location scraped — rely on the post-text Canada filter already applied

    # ── Fake profile detection ────────────────────────────────────
    signals = _scrape_profile_signals(driver)
    is_fake, fake_reasons = _is_suspicious_profile(signals)
    logger.debug(
        f"    🔍 Profile signals: connections={signals.get('connections', '?')}, "
        f"photo={signals.get('has_photo')}, headline={signals.get('headline_len', 0)} chars, "
        f"about={signals.get('has_about')}, exp={signals.get('experience_count', 0)}"
    )
    if is_fake:
        logger.info(f"    🚩 Suspicious profile — {fake_reasons}. Skipping.")
        return "skipped_fake"

    # ── Rebuild message with the REAL first name ──────────────────
    message = message.replace(f"Hi {poster_name.split()[0] if poster_name else 'there'},",
                              f"Hi {first_name},")

    contact = Contact(
        contact_id=hashlib.md5(poster_url.encode()).hexdigest()[:16],
        name=real_name,
        first_name=first_name,
        profile_url=poster_url,
        company=company,
        title=poster_title,
    )

    return _send_connection_with_note(driver, contact, message)


# ═══════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════

def hunt_hiring_posts(
    driver: webdriver.Chrome,
    db: Database,
) -> int:
    """
    Search for hiring posts, score them, and engage with legit ones.

    Returns total number of successful engagements.
    """
    if not Config.POST_HUNT_ENABLED:
        logger.info("📋 Post hunting is disabled (POST_HUNT_ENABLED=false)")
        return 0

    # ── Weekly safety check ───────────────────────────────────────
    weekly_engagements = db.weekly_post_engagements()
    if weekly_engagements >= Config.POST_HUNT_MAX_PER_WEEK:
        logger.warning(
            f"🛑 Weekly post engagement limit reached "
            f"({weekly_engagements}/{Config.POST_HUNT_MAX_PER_WEEK}). Skipping."
        )
        return 0

    weekly_connections = db.weekly_connections_sent()
    remaining_connections = Config.MAX_CONNECTIONS_PER_WEEK - weekly_connections

    logger.info("=" * 50)
    logger.info("🔍 POST HUNTER — Searching for hiring posts")
    logger.info(
        f"📊 Budget: {Config.POST_HUNT_MAX_PER_RUN}/run, "
        f"{Config.POST_HUNT_MAX_PER_WEEK - weekly_engagements} remaining this week, "
        f"{remaining_connections} connections remaining"
    )
    logger.info("=" * 50)

    # ── Collect posts from multiple keyword searches ──────────────
    all_posts: list[dict] = []
    seen_urls: set[str] = set()

    # Pick a random subset of keywords each run (looks human, covers variety)
    # With dynamic keyword generation we have 200+ keywords — pick 8-12
    # different searches per run for good coverage without hammering LinkedIn.
    keywords = Config.POST_HUNT_KEYWORDS[:]
    random.shuffle(keywords)
    keywords_to_search = keywords[:random.randint(8, min(12, len(keywords)))]

    # ── Two-pass strategy ─────────────────────────────────────────
    # Pass 1: "past-24h" — freshest posts get discovered & ranked first.
    # Pass 2: "past-week" — broader sweep picks up anything older we missed.
    # Dedup via seen_urls means the 24h posts keep their earlier position.
    date_passes = [
        ("past-24h",  "⚡ Pass 1/2 — last 24 hours (freshest first)"),
        ("past-week", "📅 Pass 2/2 — past week (broader sweep)"),
    ]

    global_kw_idx = 0  # running counter across both passes for delay logic
    total_searches = len(keywords_to_search) * len(date_passes)

    for date_filter, pass_label in date_passes:
        logger.info(f"  {pass_label}")

        # ── Anti-detection: check session is still safe ─────────
        if not is_session_safe():
            logger.critical("🛑 LinkedIn warning detected — stopping post hunt!")
            break

        for kw_idx, keyword in enumerate(keywords_to_search):
            logger.info(f"  🔎 [{date_filter}] Searching posts: \"{keyword}\"")
            try:
                url = _build_post_search_url(keyword, date_filter=date_filter)
                driver.get(url)
                human_delay(1.5, 2.5)

                # ── Anti-detection: check for warnings ─────────────
                get_session().record_search()
                warning, reason = check_for_linkedin_warnings(driver)
                if warning:
                    logger.critical(f"🛑 Warning during post search: {reason}")
                    break

                # ── Human: read the page before scrolling ───────────
                _simulate_search_reading(driver)

                # ── Scroll to load lazy-loaded posts (human-like) ───
                _scroll_feed_for_posts(driver)

                # ── Expand truncated posts to get full text ─────────
                _click_all_more_buttons(driver)
                human_delay(0.5, 1.0)

                posts_page1 = _extract_posts_from_page(driver)

                # ── Try loading page 2 (pagination / show more) ─────
                posts_page2 = []
                if _click_show_more_or_next(driver):
                    _scroll_feed_for_posts(driver)
                    _click_all_more_buttons(driver)
                    human_delay(0.5, 1.0)
                    posts_page2 = _extract_posts_from_page(driver)

                posts = posts_page1 + posts_page2
                logger.info(
                    f"    → Found {len(posts)} posts"
                    + (f" (pg1: {len(posts_page1)}, pg2: {len(posts_page2)})" if posts_page2 else "")
                )

                for p in posts:
                    post_key = p.get("post_url") or p.get("poster_url", "") + p.get("post_text", "")[:100]
                    if post_key and post_key not in seen_urls:
                        seen_urls.add(post_key)
                        all_posts.append(p)

            except Exception as e:
                logger.debug(f"  Error searching for '{keyword}': {e}")

            # ── Anti-detection: variable pause between searches ─────
            global_kw_idx += 1
            if global_kw_idx < total_searches:
                roll = random.random()
                if roll < 0.70:
                    human_delay(2.0, 4.0)     # 70% — normal pace
                elif roll < 0.92:
                    human_delay(5.0, 9.0)     # 22% — reading something
                else:
                    human_delay(10.0, 18.0)   #  8% — brief distraction

    logger.info(f"📋 Total unique posts found: {len(all_posts)}")

    # ── Spam-ring detection: identical posts from 3+ accounts ─────
    # Staffing farms coordinate fake accounts to post the exact same text.
    from collections import Counter
    text_fingerprints = Counter(
        p.get("post_text", "")[:150].strip().lower() for p in all_posts
    )
    spam_fingerprints = {t for t, c in text_fingerprints.items() if c >= 3 and t}
    if spam_fingerprints:
        before = len(all_posts)
        all_posts = [
            p for p in all_posts
            if p.get("post_text", "")[:150].strip().lower() not in spam_fingerprints
        ]
        removed = before - len(all_posts)
        logger.info(
            f"  🚫 Spam ring detected: {removed} copy-paste posts from "
            f"{len(spam_fingerprints)} duplicate text(s) removed"
        )

    if not all_posts:
        logger.info("  No hiring posts found this run.")
        return 0

    # ── Score and rank all posts ──────────────────────────────────
    scored_posts: list[tuple[int, dict]] = []
    for post in all_posts:
        # Generate post ID for dedup
        post_id = hashlib.md5(
            (post.get("poster_url", "") + post.get("post_text", "")[:200]).encode()
        ).hexdigest()[:16]

        # Skip if already engaged
        if db.hiring_post_exists(post_id):
            logger.debug(f"  Already engaged with post by {post['poster_name']}, skipping")
            continue

        # ── Hard Canada filter: the ROLE must be specifically in Canada ──
        if not _is_canadian_role(
            post.get("post_text", ""),
            post.get("poster_title", ""),
            post.get("company", ""),
        ):
            logger.debug(
                f"  🌍 Skipping {post['poster_name']} — role not specifically Canadian"
            )
            continue

        # ── Seniority filter: skip senior/staff/principal/lead roles ──
        if _is_senior_role(
            post.get("post_text", ""),
            post.get("poster_title", ""),
        ):
            logger.debug(
                f"  🎓 Skipping {post['poster_name']} — senior+ role (not our level)"
            )
            continue

        score = score_post(
            post.get("poster_title", ""),
            post.get("post_text", ""),
            post.get("company", ""),
        )

        post["_id"] = post_id
        post["_score"] = score
        scored_posts.append((score, post))

    # Sort by score descending
    scored_posts.sort(key=lambda x: x[0], reverse=True)

    # Log top 10 for visibility
    logger.info("📊 Top scored posts:")
    for i, (score, p) in enumerate(scored_posts[:10]):
        logger.info(
            f"  {i+1}. [{score:3d}pts] {p['poster_name'][:25]:25s} "
            f"| {p.get('poster_title', '')[:30]:30s} "
            f"| {p.get('company', '')[:20]:20s} "
            f"| {p['post_text'][:60]}…"
        )

    # ── Engage with top posts above threshold ─────────────────────
    engaged = 0
    connections_from_posts = 0
    visited_poster_urls: set[str] = set()  # avoid visiting same profile twice

    for score, post in scored_posts:
        if engaged >= Config.POST_HUNT_MAX_PER_RUN:
            logger.info(f"🛑 Hit per-run engagement limit ({Config.POST_HUNT_MAX_PER_RUN})")
            break
        if (weekly_engagements + engaged) >= Config.POST_HUNT_MAX_PER_WEEK:
            logger.info("🛑 Weekly post engagement limit reached. Stopping.")
            break
        if (weekly_connections + connections_from_posts) >= Config.MAX_CONNECTIONS_PER_WEEK:
            logger.info("🛑 Weekly connection limit reached. Stopping.")
            break

        if score < Config.POST_HUNT_MIN_SCORE:
            logger.debug(
                f"  ⏭ Skipping {post['poster_name']} (score {score} < "
                f"threshold {Config.POST_HUNT_MIN_SCORE})"
            )
            continue

        poster_url = post.get("poster_url", "")
        if not poster_url:
            continue

        # Skip if we already visited this person's profile this run
        if poster_url in visited_poster_urls:
            logger.debug(f"  ⏭ Already visited {post.get('poster_name', '?')}'s profile this run")
            continue
        visited_poster_urls.add(poster_url)

        poster_name = post.get("poster_name", "there")
        first_name = poster_name.split()[0] if poster_name else "there"
        company = post.get("company", "your company")
        post_text = post.get("post_text", "")

        logger.info(
            f"  💬 Engaging with {poster_name} @ {company} "
            f"(score: {score})"
        )

        # Build the DM message — extract role & tech FROM THE POST
        role_hint = _extract_role_hint(post_text)
        tech_hint = _extract_tech_hint(post_text)
        logger.info(f"    📝 Role: {role_hint} | Tech: {tech_hint}")
        template = random.choice(_POST_DM_TEMPLATES)
        message = template.format(
            poster_name=first_name,
            company=company or "your company",
            role_hint=role_hint,
            tech_hint=tech_hint,
            your_name=Config.YOUR_NAME,
        )
        if len(message) > 300:
            message = message[:297] + "..."

        logger.debug(f"  📝 Message ({len(message)} chars): {message[:80]}…")

        # ── Anti-detection: session safety gate ─────────────────
        if not is_session_safe():
            logger.critical("🛑 Session unsafe — stopping engagement loop!")
            break

        if should_take_break():
            logger.info("☕ Taking a natural break before next engagement…")
            simulate_natural_break(driver)

        # ── Anti-detection: random skip 5% of posts ──────────────
        if random.random() < 0.05:
            logger.info(f"  ⏭ Randomly skipping (anti-pattern)")
            continue

        # Log the post to DB regardless of engagement outcome
        db.insert_hiring_post(
            post_id=post["_id"],
            poster_name=poster_name,
            poster_title=post.get("poster_title", ""),
            poster_url=poster_url,
            company=company,
            post_text=post_text[:500],
            post_url=post.get("post_url", ""),
            score=score,
        )
        db.log_activity("profile_view", poster_url)

        # Send connection request or DM (reuses messenger.py logic)
        result = _engage_poster(
            driver, poster_url, poster_name,
            post.get("poster_title", ""), company, message,
        )

        if result == "skipped_not_canada":
            db.mark_post_skipped(post["_id"], "skipped_geo")
            human_delay(1, 2)  # brief pause, don't burn budget
            continue

        if result == "skipped_fake":
            db.mark_post_skipped(post["_id"], "skipped_fake")
            human_delay(1, 2)
            continue

        if result in ("connection_sent", "dm_sent"):
            db.mark_post_engaged(post["_id"], result)
            if result == "connection_sent":
                db.log_activity("connection_request", f"post_hunt:{poster_name}")
                get_session().record_connection()
                connections_from_posts += 1
            else:
                db.log_activity("direct_message", f"post_hunt:{poster_name}")
                get_session().record_dm()
            engaged += 1
            logger.info(
                f"  ✅ {result} to {poster_name} @ {company}"
            )
            smart_delay()  # fatigue-aware delay instead of fixed long_delay

            # ── Warning check after every successful engagement ──
            warning, reason = check_for_linkedin_warnings(driver)
            if warning:
                logger.critical(f"🛑 Warning after engagement: {reason}")
                break
        else:
            db.mark_post_engaged(post["_id"], "attempted")
            logger.warning(f"  ⚠️  Could not engage with {poster_name}")
            human_delay(2, 4)

    logger.info("=" * 50)
    logger.info(
        f"🏁 Post Hunter complete: {engaged} engagements "
        f"({connections_from_posts} new connections)"
    )
    logger.info("=" * 50)

    return engaged

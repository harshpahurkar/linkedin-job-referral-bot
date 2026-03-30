"""
Messenger — finds employees at target companies on LinkedIn
and sends referral request messages.
"""

import hashlib
import random
import re
import urllib.parse
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    ElementClickInterceptedException,
    WebDriverException,
)

from config import Config
from models import Job, Contact, Database
from utils import (
    get_logger, human_delay, long_delay, scroll_page,
    human_move_and_click, simulate_random_mouse_movement,
)
from antidetect import (
    get_session, is_session_safe, check_for_linkedin_warnings,
    smart_delay, should_take_break, simulate_natural_break,
    realistic_profile_reading, realistic_typing, safe_get,
)

logger = get_logger("messenger")

# Title keywords that indicate a good referral contact
_GOOD_TITLE_KEYWORDS = [
    "engineer", "developer", "swe", "sde", "programmer",
    "architect", "devops", "sre", "platform",
    "recruiter", "talent", "hiring",
    "manager", "lead", "director",
]

# Only message people whose title contains at least one of these.
# These are people in Harsh's field who can actually give a referral.
_RELEVANT_TITLE_KEYWORDS = [
    # Technical roles (peers who understand your work)
    "engineer", "developer", "programmer", "architect",
    "swe", "sde", "sdet", "devops", "sre", "platform",
    "software", "backend", "frontend", "full-stack", "fullstack",
    "full stack", "cloud", "infrastructure", "automation", "qa",
    "technical", "tech lead", "cto", "vp of engineering",
    # Hiring/recruiting roles
    "recruiter", "recruiting", "talent", "hiring",
    "people operations", "human resource",
    # Management with hiring influence
    "engineering manager", "director of engineering",
    "head of engineering", "vp engineering",
]


def find_and_message_employees(
    driver: webdriver.Chrome,
    db: Database,
    jobs: list[Job],
) -> int:
    """
    For each new job, search LinkedIn for employees at that company
    and send a referral request. Returns total messages sent.
    """
    total_sent = 0
    connections_today = 0  # track new connection requests (DMs don't count toward weekly limit)
    companies_processed: set[str] = set()

    # ── Weekly safety checks ──────────────────────────────────────
    weekly_connections = db.weekly_connections_sent()
    weekly_profiles = db.weekly_profiles_viewed()
    if weekly_connections >= Config.MAX_CONNECTIONS_PER_WEEK:
        logger.warning(
            f"🛑 Weekly connection limit already reached "
            f"({weekly_connections}/{Config.MAX_CONNECTIONS_PER_WEEK}). "
            f"Skipping outreach to protect your account."
        )
        return 0
    if weekly_profiles >= Config.MAX_PROFILE_VIEWS_PER_WEEK:
        logger.warning(
            f"🛑 Weekly profile-view limit reached "
            f"({weekly_profiles}/{Config.MAX_PROFILE_VIEWS_PER_WEEK}). "
                f"Skipping outreach to protect your account. "
                f"Wait for the 7-day window to roll over or clear old weekly_activity rows."
        )
        return 0

    logger.info(
        f"📊 Weekly stats: {weekly_connections}/{Config.MAX_CONNECTIONS_PER_WEEK} "
        f"connections, {weekly_profiles}/{Config.MAX_PROFILE_VIEWS_PER_WEEK} profile views"
    )

    for job in jobs:
        if total_sent >= Config.MAX_MESSAGES_PER_DAY:
            logger.info(f"🛑 Daily message limit reached ({Config.MAX_MESSAGES_PER_DAY}).")
            break
        if (weekly_connections + connections_today) >= Config.MAX_CONNECTIONS_PER_WEEK:
            logger.info("🛑 Weekly connection limit reached. Stopping.")
            break

        # ── Anti-detection: session safety check ─────────────────
        if not is_session_safe():
            logger.critical("🛑 LinkedIn warning detected — stopping outreach immediately!")
            break

        # ── Anti-detection: micro-break every N actions ──────────
        if should_take_break():
            simulate_natural_break(driver)
            if not is_session_safe():
                logger.critical("🛑 Warning detected during break — aborting!")
                break

        company = job.company.strip()
        if not company or company == "Unknown" or company in companies_processed:
            continue

        # ── Anti-detection: skip ~5% of companies randomly ──────────
        # Breaks the exhaustive crawl pattern that bots exhibit.
        # Kept low (5%) because natural skipping already happens often.
        if random.random() < 0.05:
            logger.info(f"  ⏭ Randomly skipping {company} (anti-pattern)")
            continue

        companies_processed.add(company)
        logger.info(f"🏢 Looking for employees at: {company} (job loc: {job.location})")

        # Try company /people/ page first (more natural), fall back to search
        contacts = _browse_company_people_page(driver, db, company, job=job)
        if not contacts:
            contacts = _search_company_employees(driver, company, job=job)
        if not contacts:
            logger.info(f"  → No reachable contacts found at {company}")
            continue

        # Filter out low-value contacts (students, interns, freelancers)
        contacts = _filter_contacts(contacts)

        # Sort by relevance score — best contacts first.
        # Main loop caps at MAX_MESSAGES_PER_COMPANY successful sends.
        contacts.sort(key=_score_contact, reverse=True)

        sent_at_company = 0
        consecutive_failures = 0
        _MAX_CONSECUTIVE_FAILURES = 3
        for contact in contacts:
            if total_sent >= Config.MAX_MESSAGES_PER_DAY:
                break
            if (weekly_connections + connections_today) >= Config.MAX_CONNECTIONS_PER_WEEK:
                break
            if sent_at_company >= Config.MAX_MESSAGES_PER_COMPANY:
                logger.debug(f"  → Hit per-company limit ({Config.MAX_MESSAGES_PER_COMPANY}) for {company}, moving on.")
                break
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.info(f"  → {_MAX_CONSECUTIVE_FAILURES} consecutive failures at {company}, moving on.")
                break

            # Skip if already messaged
            if db.already_messaged(contact.contact_id):
                logger.debug(f"  → Already messaged {contact.name}, skipping.")
                continue

            # Skip contacts with no profile URL (bad extraction)
            if not contact.profile_url:
                logger.debug(f"  → No profile URL for {contact.name}, skipping.")
                continue

            db.insert_contact(contact)

            # Track profile view
            db.log_activity("profile_view", contact.profile_url)

            # ── Human pattern: browse-without-acting ─────────────
            # Real humans view some profiles and move on without
            # connecting. A bot that connects with 100% of viewed
            # profiles is suspicious. ~15% of the time, we just
            # read the profile and skip (still counts as a view).
            if random.random() < 0.15:
                logger.info(f"  👀 Browsed {contact.name}'s profile without acting (human pattern)")
                # Still visit the profile so it looks natural
                try:
                    if not safe_get(driver, contact.profile_url):
                        continue
                    get_session().record_profile_view()
                    realistic_profile_reading(driver)
                except Exception:
                    pass
                human_delay(1.0, 3.0)
                continue

            message = _pick_message(contact, job)

            result = _send_connection_with_note(driver, contact, message)
            if result in ("connection_sent", "dm_sent"):
                db.mark_messaged(contact.contact_id)
                db.mark_referral_requested(job.job_id)
                if result == "connection_sent":
                    db.log_activity("connection_request", contact.name)
                    connections_today += 1
                    get_session().record_connection()
                else:
                    db.log_activity("direct_message", contact.name)
                    get_session().record_dm()
                total_sent += 1
                sent_at_company += 1
                consecutive_failures = 0  # reset on success
                logger.info(f"  ✉️  Sent referral request to {contact.name} ({company})")

                # ── Anti-detection: fatigue-aware delay after send ──
                # Uses session velocity + fatigue multiplier for
                # realistic pacing that slows down over time.
                smart_delay(
                    Config.MESSAGE_DELAY_MIN,
                    Config.MESSAGE_DELAY_MAX,
                    action_type="connection",
                )

                # Check for warnings after each send
                warning, _ = check_for_linkedin_warnings(driver)
                if warning:
                    logger.critical("🛑 Warning after send — stopping immediately!")
                    break
            else:
                consecutive_failures += 1
                logger.warning(f"  ⚠️  Could not message {contact.name}")
                human_delay(1, 2)

    logger.info(
        f"✅ Total referral messages sent this run: {total_sent} "
        f"({connections_today} new connections, {total_sent - connections_today} DMs)"
    )
    logger.info(
        f"📊 Updated weekly totals: ~{weekly_connections + connections_today} connections, "
        f"~{weekly_profiles + len(companies_processed)} profile views"
    )
    return total_sent


# ── Internal helpers ──────────────────────────────────────────────────

_msg_counter = 0  # fallback rotation counter


# ── JD-aware tech extraction ─────────────────────────────────────────
# Technologies Harsh actually knows, categorised by domain.
# Only skills in this map will appear in messages — no hallucinating.
_MY_SKILLS: dict[str, dict[str, str]] = {
    "languages": {
        "java": "Java", "python": "Python", "javascript": "JavaScript",
        "typescript": "TypeScript", "sql": "SQL",
    },
    "frameworks": {
        "react": "React", "spring boot": "Spring Boot", "spring": "Spring",
        "node.js": "Node.js", "express": "Express",
        "django": "Django", "flask": "Flask", "angular": "Angular",
        "next.js": "Next.js", "vue": "Vue",
    },
    "cloud_devops": {
        "aws": "AWS", "azure": "Azure", "docker": "Docker",
        "kubernetes": "Kubernetes", "k8s": "Kubernetes",
        "terraform": "Terraform", "ci/cd": "CI/CD",
        "jenkins": "Jenkins",
    },
    "data": {
        "postgresql": "PostgreSQL", "postgres": "PostgreSQL",
        "mongodb": "MongoDB", "mysql": "MySQL",
        "redis": "Redis", "kafka": "Kafka",
    },
    "practices": {
        "microservices": "microservices", "rest api": "REST APIs",
        "restful": "REST APIs", "selenium": "Selenium",
    },
}


def _extract_tech_from_jd(job: Job) -> str:
    """Build a short tech-stack snippet from the JD matching Harsh's skills.

    Returns e.g. "Python, React, and AWS".  Used in JD-aware templates
    so every message feels tailored to the posting.  Falls back to a
    generic snippet when the JD has no recognisable tech.
    """
    text = f"{job.description} {job.title}".lower()

    # Gather matches per category (deduplicate display names)
    found: dict[str, list[str]] = {}
    seen: set[str] = set()
    for category, techs in _MY_SKILLS.items():
        for keyword, display in techs.items():
            if keyword in text and display not in seen:
                found.setdefault(category, []).append(display)
                seen.add(display)

    # Pick one from each category for diversity, up to 3
    picked: list[str] = []
    for cat in ("languages", "frameworks", "cloud_devops", "data", "practices"):
        options = found.get(cat, [])
        if options and len(picked) < 3:
            picked.append(random.choice(options))

    # Pad from remaining matches if we found fewer than 2
    if len(picked) < 2:
        remaining = [d for opts in found.values() for d in opts if d not in picked]
        random.shuffle(remaining)
        for d in remaining:
            if d not in picked:
                picked.append(d)
            if len(picked) >= 3:
                break

    if not picked:
        return random.choice([
            "Java, Python, and cloud tools",
            "Python, React, and AWS",
            "Java, Spring Boot, and microservices",
            "Python, Docker, and CI/CD",
        ])

    if len(picked) == 1:
        return picked[0]
    if len(picked) == 2:
        return f"{picked[0]} and {picked[1]}"
    return f"{picked[0]}, {picked[1]}, and {picked[2]}"


# ── Template pool indices for job-title matching ─────────────────────
# T0–T4 = originals (hardcoded tech), T5–T6 = JD-aware, T7 = recruiter.
# T2 = school alum.
_SCHOOL_IDX    = 2
_RECRUITER_IDX = 7
_FULLSTACK_POOL = [0, 5, 6]
_BACKEND_POOL   = [1, 5, 6]
_CLOUD_POOL     = [3, 5, 6]
_QA_POOL        = [0, 5, 6]
_GENERAL_POOL   = [4, 5, 6]


def _pick_message(contact: Contact, job: Job) -> str:
    """
    Select and format the best message template for this contact + job.

    Strategy (in priority order):
      1. School alum  → T2 (shared-school hook)
      2. Recruiter / talent contact → T12 (recruiter-specific)
      3. Job-title keyword match → rotate through a pool of original
         + JD-aware templates so LinkedIn never sees the same
         message twice in a row.
      4. Fallback → rotate through the general pool.

    JD-aware templates (T5–T12) include a {tech_snippet} placeholder
    that gets filled with technologies extracted from the actual job
    description, making every message feel tailored to the posting.

    All messages are capped at 300 chars (LinkedIn's hard limit).
    """
    global _msg_counter

    templates = Config.REFERRAL_TEMPLATES

    # ── 1. School alum check ─────────────────────────────────────
    contact_text = f"{contact.title} {contact.name}".lower()
    is_alum = Config.YOUR_SCHOOL and Config.YOUR_SCHOOL.lower() in contact_text

    if is_alum and _SCHOOL_IDX < len(templates):
        template = templates[_SCHOOL_IDX]

    # ── 2. Recruiter / talent contact ────────────────────────────
    elif any(kw in (contact.title or "").lower() for kw in _RECRUITER_KEYWORDS):
        if _RECRUITER_IDX < len(templates):
            template = templates[_RECRUITER_IDX]
        else:
            template = templates[4]  # safe fallback

    # ── 3. Match job title → template pool ───────────────────────
    else:
        jt = job.title.lower()

        if any(kw in jt for kw in [
            "full-stack", "full stack", "fullstack", "frontend",
            "front-end", "front end", "react", "angular", "vue",
        ]):
            pool = _FULLSTACK_POOL
        elif any(kw in jt for kw in [
            "backend", "back-end", "back end", "microservice",
            "api", "java", "spring", "node", "express",
        ]):
            pool = _BACKEND_POOL
        elif any(kw in jt for kw in [
            "cloud", "devops", "dev ops", "infrastructure", "infra",
            "platform", "sre", "site reliability", "kubernetes",
            "aws", "azure", "gcp",
        ]):
            pool = _CLOUD_POOL
        elif any(kw in jt for kw in [
            "automation", "qa", "quality", "sdet", "test",
            "selenium", "cypress",
        ]):
            pool = _QA_POOL
        else:
            pool = _GENERAL_POOL

        valid_pool = [i for i in pool if i < len(templates)]
        template = templates[valid_pool[_msg_counter % len(valid_pool)]]
        _msg_counter += 1

    # ── Build the tech snippet from the JD ───────────────────────
    tech_snippet = _extract_tech_from_jd(job)

    # Truncate job title if it's too long (to keep msg under 300 chars)
    job_title = job.title
    if len(job_title) > 40:
        # Cut at last space before 40 chars so we don't chop mid-word
        cut = job_title[:40].rfind(" ")
        if cut > 10:
            job_title = job_title[:cut]
        else:
            job_title = job_title[:40]

    msg = template.format(
        first_name=contact.first_name,
        job_title=job_title,
        company=job.company,
        your_name=Config.YOUR_NAME,
        school=Config.YOUR_SCHOOL,
        tech_snippet=tech_snippet,
    )

    # Hard cap at 300 characters
    if len(msg) > 300:
        msg = msg[:297] + "..."

    logger.debug(
        f"  📝 Message for {contact.name}: role='{job_title}' "
        f"company='{job.company}' tech='{tech_snippet}' [{len(msg)} chars]"
    )
    return msg


def _score_contact(contact: Contact) -> int:
    """
    Score a contact by how likely they are to provide a referral.
    Higher = better.
      - Engineers/devs: high (they understand your pain)
      - Recruiters/talent: high (they're literally hiring)
      - Managers/leads: medium-high (they influence hiring)
      - Everyone else: low
    """
    title = (contact.title or "").lower()
    score = 0
    for kw in _GOOD_TITLE_KEYWORDS:
        if kw in title:
            score += 10
            break

    # Bonus for recruiter/talent (they're always looking)
    if any(w in title for w in ("recruiter", "talent", "hiring")):
        score += 15

    # Bonus for senior+ (more influence)
    if any(w in title for w in ("senior", "lead", "principal", "staff", "manager", "director")):
        score += 5

    return score


def _filter_relevant_titles(contacts: list[Contact]) -> list[Contact]:
    """
    Only keep contacts whose title contains at least one relevant keyword.
    This ensures we message engineers, recruiters, hiring managers —
    not random sales/marketing/design people who can't refer us.
    """
    relevant = []
    for c in contacts:
        title_lower = (c.title or "").lower()
        if not title_lower:
            continue  # no title at all — skip
        if any(kw in title_lower for kw in _RELEVANT_TITLE_KEYWORDS):
            relevant.append(c)
        else:
            logger.debug(f"  ⏭ Skipping {c.name} — irrelevant title: {c.title}")
    return relevant


# Keywords that identify recruiting/talent roles
_RECRUITER_KEYWORDS = [
    "recruiter", "recruiting", "talent", "hiring",
    "people operations", "human resource",
]


def _split_contacts_by_role(
    contacts: list[Contact],
    max_recruiters: int = 1,
    max_technical: int = 4,
) -> list[Contact]:
    """
    Split contacts into recruiters and technical people, then combine.
    Returns up to max_recruiters recruiters + max_technical engineers/devs.
    This ensures we mostly message peers who understand our work,
    with 1 recruiter as a bonus.
    """
    recruiters: list[Contact] = []
    technical: list[Contact] = []

    for c in contacts:
        title_lower = (c.title or "").lower()
        if any(kw in title_lower for kw in _RECRUITER_KEYWORDS):
            recruiters.append(c)
        else:
            technical.append(c)

    # Score each bucket separately so we get the best of each
    recruiters.sort(key=_score_contact, reverse=True)
    technical.sort(key=_score_contact, reverse=True)

    picked = technical[:max_technical] + recruiters[:max_recruiters]
    return picked


def _filter_contacts(contacts: list[Contact]) -> list[Contact]:
    """
    Filter out contacts whose title contains block words
    (students, interns, freelancers, etc.) — they can't give referrals.
    """
    if not Config.CONTACT_BLOCK_WORDS:
        return contacts

    filtered = []
    for c in contacts:
        title_lower = (c.title or "").lower()
        name_lower = (c.name or "").lower()
        blocked = False
        for word in Config.CONTACT_BLOCK_WORDS:
            if word and (word in title_lower or word in name_lower):
                logger.debug(f"  🚫 Filtering out {c.name} (matched block word: '{word}')")
                blocked = True
                break
        if not blocked:
            filtered.append(c)

    if len(filtered) < len(contacts):
        logger.info(
            f"  → Filtered {len(contacts) - len(filtered)} low-value contacts "
            f"(students/interns/freelancers)"
        )
    return filtered


# ── Geography helpers ─────────────────────────────────────────────────

# Canadian province / territory codes + common location strings
_CANADA_LOCATION_KEYWORDS = [
    "canada",
    # Provinces
    "ontario", ", on", "toronto", "ottawa", "waterloo", "kitchener",
    "mississauga", "hamilton", "london, on", "brampton", "markham",
    "british columbia", ", bc", "vancouver", "victoria, bc", "burnaby", "surrey",
    "quebec", ", qc", "montreal", "montréal", "québec", "laval",
    "alberta", ", ab", "calgary", "edmonton",
    "manitoba", ", mb", "winnipeg",
    "saskatchewan", ", sk", "saskatoon", "regina",
    "nova scotia", ", ns", "halifax",
    "new brunswick", ", nb", "moncton", "fredericton",
    "newfoundland", ", nl", "st. john's",
    "prince edward island", ", pe", "charlottetown",
]

# Broader North America (US states, etc.) — used if no Canadian contacts found
_NA_LOCATION_KEYWORDS = _CANADA_LOCATION_KEYWORDS + [
    "united states", ", us",
    "new york", "san francisco", "seattle", "austin", "chicago",
    "boston", "los angeles", "denver", "atlanta", "dallas",
    "washington", "portland", "philadelphia", "miami",
    "north america",
]


def _is_canadian_location(location: str) -> bool:
    """Check if a location string looks Canadian."""
    loc = location.lower()
    return any(kw in loc for kw in _CANADA_LOCATION_KEYWORDS)


def _is_north_american_location(location: str) -> bool:
    """Check if a location string looks North American (CA or US)."""
    loc = location.lower()
    return any(kw in loc for kw in _NA_LOCATION_KEYWORDS)


def _is_remote_or_us_job(job: Job | None) -> bool:
    """Check if a job is US-based or a remote role with no clear Canadian location.

    For these jobs, we accept North American contacts rather than
    filtering strictly for Canadian ones.
    """
    if not job:
        return False
    loc = job.location.lower()
    # Clearly US-based
    if "united states" in loc or ", us" in loc:
        return True
    # US city names in location
    _US_CITIES = ["new york", "san francisco", "seattle", "austin", "chicago",
                  "boston", "los angeles", "denver", "atlanta", "dallas",
                  "washington", "portland", "philadelphia", "miami"]
    if any(city in loc for city in _US_CITIES):
        return True
    # "Remote" without any Canadian indicator → likely US remote
    if "remote" in loc and not _is_canadian_location(loc):
        return True
    return False


def _company_name_matches(expected: str, found: str) -> bool:
    """
    Check if a found company name is a reasonable match for the expected one.
    Handles cases like 'Unity' vs 'Unity Technologies' or 'IBM' vs 'IBM Canada'.
    """
    return _company_match_score(expected, found) >= 45


def _normalize_company_name(name: str) -> str:
    """Normalize a company name for fuzzy matching across LinkedIn variants."""
    if not name:
        return ""

    text = name.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t]

    # Common legal suffixes / noise that hurt matching quality.
    stopwords = {
        "inc", "incorporated", "corp", "corporation", "co", "company",
        "ltd", "limited", "llc", "plc", "group", "holdings", "technologies",
        "technology", "solutions", "services", "international", "global",
        "the", "and",
    }
    filtered = [t for t in tokens if t not in stopwords]
    if filtered:
        tokens = filtered

    return " ".join(tokens)


def _company_match_score(expected: str, found: str) -> int:
    """Return 0-100 match score between expected company and a found candidate."""
    e_raw = expected.lower().strip()
    f_raw = found.lower().strip()
    if not e_raw or not f_raw:
        return 0

    e = _normalize_company_name(expected)
    f = _normalize_company_name(found)

    if not e or not f:
        return 0
    if e == f:
        return 100
    if e in f or f in e:
        return 82

    e_tokens = e.split()
    f_tokens = f.split()
    e_set = set(e_tokens)
    f_set = set(f_tokens)
    overlap = len(e_set & f_set)
    if overlap == 0:
        # Short-name fallback ("ibm" vs "ibm canada")
        if len(e) <= 5 and (f.startswith(e) or e.startswith(f)):
            return 60
        return 0

    score = int((overlap / max(len(e_set), len(f_set))) * 100)
    if e_tokens and f_tokens and e_tokens[0] == f_tokens[0]:
        score += 10
    return min(score, 95)


def _extract_people_from_current_page(
    driver: webdriver.Chrome,
    source_label: str,
) -> list[dict[str, str]]:
    """DOM-agnostic fallback extractor for profile rows on people/search pages."""
    try:
        people_data = driver.execute_script(r"""
            const out = [];
            const seen = new Set();
            const profilePathRe = /\/in\/[a-z0-9\-_%]+\/?$/i;

            function visible(el) {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return el.offsetParent !== null && r.width > 0 && r.height > 0;
            }

            function clean(s) {
                return (s || '').replace(/\s+/g, ' ').trim();
            }

            const links = document.querySelectorAll('a[href*="/in/"]');
            for (const link of links) {
                if (!visible(link)) continue;

                const href = (link.href || '').split('?')[0].replace(/\/$/, '');
                if (!href || seen.has(href)) continue;
                if (!profilePathRe.test(href)) continue;
                if (href.includes('/in/me')) continue;

                let card = link.closest(
                    '.reusable-search__result-container, .artdeco-entity-lockup, '
                    + '.org-people-profile-card, li, article, .entity-result'
                );
                if (!card) {
                    let el = link;
                    for (let i = 0; i < 7; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const txt = clean(el.innerText || '');
                        if (txt.split('\n').length >= 2) {
                            card = el;
                            break;
                        }
                    }
                }

                const nameRaw = clean(link.innerText || link.textContent || '');
                if (!nameRaw || nameRaw.toLowerCase() === 'linkedin member') continue;
                if (nameRaw.length > 80) continue;

                const lines = card
                    ? (card.innerText || '').split('\n').map(clean).filter(Boolean)
                    : [nameRaw];

                const deduped = [];
                const lineSeen = new Set();
                for (const line of lines) {
                    const k = line.toLowerCase();
                    if (!lineSeen.has(k)) {
                        lineSeen.add(k);
                        deduped.push(line);
                    }
                }

                let title = '';
                let location = '';
                for (const line of deduped) {
                    const low = line.toLowerCase();
                    if (low === nameRaw.toLowerCase()) continue;
                    if (!location && (
                        line.includes(',') ||
                        /\b(canada|united states|remote|area|province|state)\b/i.test(low)
                    )) {
                        location = line;
                        continue;
                    }
                    if (!title && line.length >= 3 && line.length <= 130
                        && !/^follow$/i.test(line)
                        && !/^connect$/i.test(line)
                        && !/\b\d+(st|nd|rd|th)\+?\b/.test(line)) {
                        title = line;
                    }
                }

                out.push({
                    name: nameRaw,
                    title: title,
                    location: location,
                    link: href,
                });
                seen.add(href);

                if (out.length >= 150) break;
            }

            return out;
        """)
        if people_data:
            logger.debug(f"  {source_label}: structural fallback extracted {len(people_data)} profiles")
            return people_data
    except Exception as e:
        logger.debug(f"  {source_label}: structural fallback extractor failed: {e}")

    return []


def _build_contacts_from_people_data(
    company: str,
    people_data: list[dict],
    max_results: int,
) -> list[Contact]:
    """Convert raw extracted people dicts to Contact records."""
    contacts: list[Contact] = []
    seen_ids: set[str] = set()

    for person in people_data:
        name = (person.get("name") or "").strip()
        title_text = (person.get("title") or "").strip()
        profile_url = (person.get("link") or "").strip()
        person_location = (person.get("location") or "").strip()

        if not name or not profile_url:
            continue

        first_name = name.split()[0] if name else "there"
        contact_id = hashlib.md5(f"{name}|{profile_url}".encode()).hexdigest()[:16]
        if contact_id in seen_ids:
            continue

        contacts.append(Contact(
            contact_id=contact_id,
            name=name,
            first_name=first_name,
            profile_url=profile_url,
            company=company,
            title=title_text,
            location=person_location,
        ))
        seen_ids.add(contact_id)

        if len(contacts) >= max_results:
            break

    return contacts


def _save_contact_debug_snapshot(driver: webdriver.Chrome, company: str, stage: str):
    """Save a screenshot when contact extraction fails, for selector debugging."""
    try:
        safe_company = re.sub(r"[^a-zA-Z0-9_-]+", "_", company)[:40]
        safe_stage = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage)[:24]
        debug_dir = Path(__file__).parent / "data" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        shot_path = debug_dir / f"contacts_{safe_company}_{safe_stage}.png"
        driver.save_screenshot(str(shot_path))
        logger.debug(f"  Saved contact debug screenshot: {shot_path}")
    except Exception:
        pass


def _browse_company_people_page(
    driver: webdriver.Chrome,
    db: Database,
    company: str,
    max_results: int = 15,
    job: Job | None = None,
) -> list[Contact]:
    """
    Navigate to the company's LinkedIn /people/ page directly.
    This is more natural-looking than running a people search.
    Filters for Canadian / North American contacts based on job location.
    Verifies the company page matches the expected company name.
    """
    contacts: list[Contact] = []
    try:
        # First, find the company page via search
        search_url = (
            f"https://www.linkedin.com/search/results/companies/"
            f"?keywords={company}&origin=GLOBAL_SEARCH_HEADER"
        )
        if not safe_get(driver, search_url):
            return contacts
        db.log_activity("profile_view", f"company_search:{company}")

        # ── Find the RIGHT company from search results ──────────────
        company_url = ""
        try:
            # Extract all company results and pick the best match
            results = driver.execute_script("""
                const results = [];
                document.querySelectorAll('.reusable-search__result-container').forEach(card => {
                    const link = card.querySelector('a[href*="/company/"]');
                    const nameEl = card.querySelector('.entity-result__title-text a span[aria-hidden="true"]')
                               || card.querySelector('.entity-result__title-text a span')
                               || card.querySelector('.entity-result__title-text a');
                    if (!link || !nameEl) return;
                    results.push({
                        url: link.href.split('?')[0],
                        name: (nameEl.innerText || nameEl.textContent || '').trim()
                    });
                });
                return results;
            """)

            # Fallback for newer LinkedIn DOM variants without entity-result classes.
            if not results:
                results = driver.execute_script("""
                    const out = [];
                    const seen = new Set();
                    const links = document.querySelectorAll('a[href*="/company/"]');
                    for (const a of links) {
                        const href = (a.href || '').split('?')[0].replace(/\\/$/, '');
                        if (!href || seen.has(href)) continue;
                        const txt = (a.innerText || a.textContent || '').trim();
                        if (!txt || txt.length > 120) continue;
                        out.push({ url: href, name: txt });
                        seen.add(href);
                        if (out.length >= 20) break;
                    }
                    return out;
                """)

            if results:
                # Find the best-matching company by fuzzy score.
                best_result = None
                best_score = -1
                for r in results:
                    score = _company_match_score(company, r.get("name", ""))
                    if score > best_score:
                        best_score = score
                        best_result = r

                if best_result and best_score >= 45:
                    company_url = best_result["url"]
                    logger.debug(
                        f"  ✅ Matched company: '{best_result['name']}' "
                        f"(score={best_score}) → {company_url}"
                    )
                else:
                    # No strong match — fall back to the top result rather than skipping.
                    found_names = [r["name"] for r in results[:5]]
                    if best_result:
                        company_url = best_result["url"]
                        logger.warning(
                            f"  ⚠️  Weak company match for '{company}' (best score={best_score}). "
                            f"Using '{best_result['name']}' and continuing. Candidates: {found_names}"
                        )
                    else:
                        logger.warning(
                            f"  ⚠️  No company results for '{company}'. Candidates: {found_names}"
                        )
        except TimeoutException:
            pass

        if not company_url:
            # Last resort: construct URL from company name
            slug = company.lower().replace(" ", "-").replace(".", "").replace(",", "")
            slug = slug.replace("inc", "").replace("ltd", "").replace("llc", "").strip("-")
            company_url = f"https://www.linkedin.com/company/{slug}"
            logger.debug(f"  Trying direct company URL: {company_url}")

        # ── Go to /people/ tab with geo filter ──────────────────────
        # LinkedIn /people/ page supports keyword filtering.
        # Add "Canada" keyword for Canadian jobs; skip for US remote roles.
        people_url = company_url.rstrip("/") + "/people/"
        job_location = (job.location if job else "").strip()
        if not _is_remote_or_us_job(job) and (
            _is_canadian_location(job_location) or "canada" in Config.JOB_LOCATION.lower()
        ):
            people_url += "?keywords=Canada"

        if not safe_get(driver, people_url):
            return contacts
        scroll_page(driver, scrolls=10)

        # Extract ALL people using JS — artdeco-entity-lockup with /in/ links
        # Also grab location (caption element) for geo filtering
        people_data = driver.execute_script("""
            const results = [];
            document.querySelectorAll('.artdeco-entity-lockup').forEach(card => {
                const linkEl = card.querySelector('a[href*="/in/"]');
                if (!linkEl) return;
                const titleEl = card.querySelector('.artdeco-entity-lockup__title');
                const subtitleEl = card.querySelector('.artdeco-entity-lockup__subtitle');
                const captionEl = card.querySelector('.artdeco-entity-lockup__caption');
                const name = titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '';
                const subtitle = subtitleEl ? (subtitleEl.innerText || subtitleEl.textContent || '').trim() : '';
                const location = captionEl ? (captionEl.innerText || captionEl.textContent || '').trim() : '';
                const link = linkEl.href.split('?')[0];
                if (name && name.toLowerCase() !== 'linkedin member') {
                    results.push({name: name, title: subtitle, location: location, link: link});
                }
            });
            return results;
        """)

        if not people_data:
            logger.debug(f"  No people found via strict selector on {company} people page")
            people_data = _extract_people_from_current_page(
                driver,
                source_label=f"{company} people page",
            )

        if not people_data:
            logger.debug(f"  No people found via fallback extraction on {company} people page")
            _save_contact_debug_snapshot(driver, company, "people_page_empty")
            return contacts

        logger.debug(f"  Raw people found: {len(people_data)}")

        all_contacts = _build_contacts_from_people_data(
            company,
            people_data,
            max_results=max(max_results * 4, 50),
        )
        if not all_contacts:
            logger.debug(f"  Could not build valid contact objects for {company}")
            _save_contact_debug_snapshot(driver, company, "people_parse_empty")
            return contacts

        # ── Geographic filtering ────────────────────────────────────
        # Prefer Canadian contacts; fall back to North American if needed
        canadian = [c for c in all_contacts if c.location and _is_canadian_location(c.location)]
        north_american = [c for c in all_contacts if c.location and _is_north_american_location(c.location)]

        if canadian:
            geo_filtered = canadian
            logger.debug(f"  🍁 {len(canadian)} Canadian contacts (out of {len(all_contacts)})")
        elif north_american:
            geo_filtered = north_american
            logger.debug(f"  🌎 {len(north_american)} North American contacts (out of {len(all_contacts)})")
        else:
            # If no location data available (company page doesn't show it), keep all
            geo_filtered = all_contacts
            logger.debug(f"  No geo data found, keeping all {len(all_contacts)} contacts")

        # Only keep people with relevant titles for our field
        relevant = _filter_relevant_titles(geo_filtered)
        if not relevant:
            # LinkedIn occasionally hides title/location snippets on people cards.
            # Degrade gracefully instead of dropping the company entirely.
            fallback = _filter_contacts(geo_filtered)
            fallback.sort(key=_score_contact, reverse=True)
            fallback = fallback[:max_results]
            if fallback:
                logger.warning(
                    f"  ⚠️  {len(all_contacts)} people found at {company} but no strict title matches; "
                    f"using {len(fallback)} fallback contacts."
                )
                return fallback

            logger.info(
                f"  → {len(all_contacts)} people found but none passed title/block filters for {company}"
            )
            return contacts

        # Sort by relevance — main loop caps at MAX_MESSAGES_PER_COMPANY
        relevant.sort(key=_score_contact, reverse=True)
        contacts = relevant[:max_results]

        logger.info(
            f"  → Found {len(contacts)} relevant contacts out of {len(all_contacts)} "
            f"(titles: {', '.join(c.title[:30] for c in contacts[:12])})"
        )

    except Exception as e:
        logger.debug(f"Company people page approach failed for {company}: {e}")

    return contacts


def _search_company_employees(
    driver: webdriver.Chrome,
    company: str,
    max_results: int = 30,
    job: Job | None = None,
) -> list[Contact]:
    """
    Search LinkedIn People for employees currently at `company`.
    Returns up to `max_results` contacts.
    Includes 1st-degree connections so we can DM already-connected people.
    """
    # Add "Canada" to keywords for Canadian jobs; skip for US remote roles
    geo_suffix = ""
    job_location = (job.location if job else "").strip()
    if not _is_remote_or_us_job(job) and (
        _is_canadian_location(job_location) or "canada" in Config.JOB_LOCATION.lower()
    ):
        geo_suffix = " Canada"

    keyword_query = urllib.parse.quote_plus(f"{company}{geo_suffix}")
    search_urls = [
        # network filter: F = 1st degree, S = 2nd degree, O = 3rd+
        (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={keyword_query}"
            f"&network=%5B%22F%22%2C%22S%22%2C%22O%22%5D"
            f"&origin=FACETED_SEARCH"
        ),
        # Fallback URL for newer search routing.
        (
            f"https://www.linkedin.com/search/results/people/"
            f"?keywords={keyword_query}"
            f"&origin=GLOBAL_SEARCH_HEADER"
        ),
    ]

    contacts: list[Contact] = []
    people_data: list[dict] = []

    for idx, search_url in enumerate(search_urls, start=1):
        try:
            if not safe_get(driver, search_url):
                continue
            scroll_page(driver, scrolls=8)
        except WebDriverException as e:
            logger.debug(f"Browser error navigating to people search (attempt {idx}): {e}")
            continue

        try:
            # Use JS to extract all people data at once — avoids stale element issues.
            people_data = driver.execute_script("""
                const results = [];
                document.querySelectorAll('.reusable-search__result-container').forEach(card => {
                    const nameEl = card.querySelector(".entity-result__title-text a span[aria-hidden='true']")
                                 || card.querySelector('.entity-result__title-text a span');
                    const linkEl = card.querySelector('.entity-result__title-text a[href*="/in/"]')
                                 || card.querySelector('a[href*="/in/"]');
                    const subtitleEl = card.querySelector('.entity-result__primary-subtitle');
                    const secondaryEl = card.querySelector('.entity-result__secondary-subtitle');
                    const summaryEl = card.querySelector('.entity-result__summary');
                    if (!nameEl) return;
                    const name = nameEl.textContent.trim();
                    if (!name || name.toLowerCase() === 'linkedin member') return;
                    let title = subtitleEl ? subtitleEl.textContent.trim() : '';
                    if (!title && summaryEl) {
                        title = summaryEl.textContent.trim().replace(/^Current:\\s*/i, '');
                    }
                    const location = secondaryEl ? secondaryEl.textContent.trim() : '';
                    const link = linkEl ? linkEl.href.split('?')[0] : '';
                    if (link) {
                        results.push({ name, link, title, location });
                    }
                });
                return results;
            """)
        except Exception:
            people_data = []

        if not people_data:
            people_data = _extract_people_from_current_page(
                driver,
                source_label=f"people search attempt {idx}",
            )

        if people_data:
            logger.debug(
                f"  Search attempt {idx}: extracted {len(people_data)} profiles for {company}"
            )
            break

    if not people_data:
        logger.debug(f"  Search fallback: no results found for {company}")
        _save_contact_debug_snapshot(driver, company, "people_search_empty")
        return contacts

    logger.debug(f"  Search fallback: {len(people_data)} people found for {company}")

    contacts = _build_contacts_from_people_data(
        company,
        people_data,
        max_results=max(max_results * 3, 40),
    )

    if not contacts:
        logger.debug(f"  Search fallback: extracted rows but no valid contacts for {company}")
        return contacts

    # Prefer relevant roles, but gracefully keep fallback contacts when title snippets are missing.
    relevant = _filter_relevant_titles(contacts)
    if relevant:
        relevant.sort(key=_score_contact, reverse=True)
        return relevant[:max_results]

    contacts = _filter_contacts(contacts)
    contacts.sort(key=_score_contact, reverse=True)
    logger.warning(
        f"  ⚠️  Search found contacts for {company} but no strict relevant-title matches; "
        f"using {min(len(contacts), max_results)} fallback contacts."
    )
    return contacts[:max_results]

    return contacts


def _send_connection_with_note(
    driver: webdriver.Chrome,
    contact: Contact,
    message: str,
) -> str:
    """
    Navigate to the contact's profile and send a Connect request
    with a personalized note, or a DM if already connected.
    Returns: 'connection_sent', 'dm_sent', or 'failed'.
    """
    try:
        if not safe_get(driver, contact.profile_url):
            return "failed"

        # ── Anti-detection: realistic profile reading ─────────────
        # Uses content-scaled timing, Bézier mouse, scroll-back patterns
        get_session().record_profile_view()
        realistic_profile_reading(driver)

        status = _get_connection_status(driver)
        logger.debug(
            f"  Profile {contact.name}: connection status = '{status}', "
            f"URL = {driver.current_url}"
        )
        if status == "connected":
            logger.info(f"  🔗 Already connected to {contact.name} — sending DM")
            return "dm_sent" if _send_direct_message(driver, contact, message) else "failed"
        if status == "pending":
            logger.debug(f"  Connection already pending for {contact.name}, skipping")
            return "failed"

        # Step 1: Find and click the Connect button (direct or inside "More")
        clicked = _click_connect_button(driver)
        if not clicked:
            logger.debug(f"  No Connect button for {contact.name}")
            return "failed"

        human_delay(0.5, 1)

        # Step 2: Look for the "Add a note" button in the connection modal
        add_note_clicked = driver.execute_script("""
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const text = btn.textContent.trim().toLowerCase();
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if ((text.includes('add a note') || label.includes('add a note'))
                    && btn.offsetParent !== null) {
                    btn.click();
                    return true;
                }
            }
            return false;
        """)

        if add_note_clicked:
            human_delay(0.5, 1)
        else:
            logger.debug("  No 'Add a note' button — may already show textarea")

        # Step 3: Find the textarea and type the message
        try:
            note_field = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     "textarea[name='message'], textarea#custom-message, "
                     "textarea.connect-button-send-invite__custom-message")
                )
            )
        except TimeoutException:
            # Fallback: any visible textarea
            note_field = driver.execute_script("""
                const areas = document.querySelectorAll('textarea');
                for (const ta of areas) {
                    if (ta.offsetParent !== null) return ta;
                }
                return null;
            """)
            if not note_field:
                logger.debug("  No textarea found — sending without note")
                return "connection_sent" if _click_send_button(driver) else "failed"

        note_field.clear()
        _type_message(note_field, message[:300])
        human_delay(0.5, 1)

        # Step 4: Click Send
        return "connection_sent" if _click_send_button(driver) else "failed"

    except Exception as e:
        logger.error(f"Error sending connection to {contact.name}: {e}")
        return "failed"


def _send_direct_message(
    driver: webdriver.Chrome,
    contact: Contact,
    message: str,
) -> bool:
    """Send a direct message to an already-connected contact.

    Clicks the Message button on their profile, types the referral message
    in the messaging overlay, and sends it.
    """
    import time

    try:
        # Click the "Message" button on the profile page
        msg_clicked = driver.execute_script("""
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const label = (btn.getAttribute('aria-label') || '');
                const rect = btn.getBoundingClientRect();
                const visible = rect.top > 0 && rect.top < 600 && btn.offsetParent !== null;
                if (label.startsWith('Message ') && visible) {
                    btn.click();
                    return true;
                }
            }
            return false;
        """)

        if not msg_clicked:
            logger.debug(f"  No Message button found on {contact.name}'s profile")
            return False

        human_delay(1.5, 3)

        # Wait for the message input to appear (overlay or full page)
        msg_input = None
        for _ in range(8):
            msg_input = driver.execute_script("""
                const selectors = [
                    'div.msg-form__contenteditable[contenteditable="true"]',
                    'div[role="textbox"][contenteditable="true"]',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) return el;
                }
                return null;
            """)
            if msg_input:
                break
            time.sleep(0.5)

        if not msg_input:
            logger.debug(f"  Message input not found for {contact.name}")
            _close_msg_overlay(driver)
            return False

        # Focus and type the message with realistic human patterns
        msg_input.click()
        human_delay(0.3, 0.5)

        realistic_typing(msg_input, message[:300])

        human_delay(0.5, 1)

        # Click Send inside the messaging form
        send_clicked = False
        for _ in range(6):
            send_clicked = driver.execute_script("""
                // Try the dedicated send button class first
                const sendBtns = document.querySelectorAll(
                    'button.msg-form__send-button, button[type="submit"]'
                );
                for (const btn of sendBtns) {
                    if (btn.offsetParent !== null && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                }
                // Fallback: any Send button inside a messaging container
                const all = document.querySelectorAll('button');
                for (const btn of all) {
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const text = btn.innerText.trim().toLowerCase();
                    const inMsg = btn.closest(
                        '.msg-form, .msg-overlay-conversation-bubble, '
                        + '.msg-s-message-list-container'
                    );
                    if (inMsg && (label === 'send' || text === 'send')
                        && btn.offsetParent !== null && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            """)
            if send_clicked:
                break
            time.sleep(0.5)

        if not send_clicked:
            logger.debug(f"  Send button not found for DM to {contact.name}")
            _close_msg_overlay(driver)
            return False

        logger.debug(f"  ✅ DM sent to {contact.name}")
        human_delay(1.5, 2.5)
        _close_msg_overlay(driver)
        return True

    except Exception as e:
        logger.debug(f"  Error sending DM to {contact.name}: {e}")
        _close_msg_overlay(driver)
        return False


def _close_msg_overlay(driver: webdriver.Chrome):
    """Close the LinkedIn messaging overlay if open."""
    try:
        driver.execute_script("""
            const close = document.querySelector(
                'button[data-control-name="overlay.close_conversation_window"], '
                + '.msg-overlay-bubble-header__control--close-btn, '
                + 'button[aria-label*="Close your conversation"]'
            );
            if (close) close.click();
        """)
    except Exception:
        pass


def _get_connection_status(driver: webdriver.Chrome) -> str:
    """
    Detect the relationship with this profile.
    Returns: 'connect' (can connect), 'connected' (already), 'pending', or 'unknown'.

    Handles three LinkedIn profile layouts:
      1. Normal:       Connect (primary) + Message + More
      2. Follow-first: Follow (primary) + Message + More (Connect inside More)
      3. Connected:    Message (primary) + More (no Connect anywhere)
    """
    status = driver.execute_script("""
        const btns = document.querySelectorAll('button');
        let hasConnect = false;
        let hasMessage = false;
        let hasPending = false;
        let hasFollowPrimary = false;

        for (const btn of btns) {
            const label = (btn.getAttribute('aria-label') || '');
            const rect = btn.getBoundingClientRect();
            const inHeader = rect.top > 0 && rect.top < 600 && btn.offsetParent !== null;

            // Direct "Invite X to connect" button
            if (label.includes('Invite') && label.includes('to connect')) {
                hasConnect = true;
            }
            // "Message <name>" button in the header area
            if (label.startsWith('Message ') && inHeader) {
                hasMessage = true;
            }
            if (label.includes('Pending') || btn.textContent.trim() === 'Pending') {
                hasPending = true;
            }
            // Detect Follow as the PRIMARY action in the header
            // (artdeco-button--primary = the filled/blue button)
            if (label.startsWith('Follow ') && inHeader
                && btn.className.includes('artdeco-button--primary')) {
                hasFollowPrimary = true;
            }
        }

        // Check the More dropdown for Connect (items exist in DOM even when closed)
        // Use broad selector: any div with class artdeco-dropdown__item and the right aria-label
        const ddItems = document.querySelectorAll(
            '.artdeco-dropdown__item[aria-label*="Invite"][aria-label*="to connect"], '
            + 'div[role="button"][aria-label*="Invite"][aria-label*="to connect"]'
        );
        if (ddItems.length > 0) hasConnect = true;

        if (hasPending) return 'pending';
        if (hasConnect) return 'connect';
        // Follow-first profile: Follow is primary + Message visible,
        // but Connect is hidden in More dropdown — NOT connected!
        if (hasFollowPrimary && hasMessage) return 'connect';
        if (hasMessage && !hasConnect && !hasFollowPrimary) return 'connected';
        return 'unknown';
    """) or "unknown"
    logger.debug(f"  Connection status: {status}")
    return status


def _click_connect_button(driver: webdriver.Chrome) -> bool:
    """
    Find and click the Connect button on a LinkedIn profile page.

    Real DOM patterns (from debug):
    - Direct button: <button aria-label="Invite X to connect"> with class
      artdeco-button--primary, visible near top of page
    - In More dropdown: <div role="button" aria-label="Invite X to connect">
      inside artdeco-dropdown__item, after clicking More actions button
    - "People also viewed" section has Connect buttons too — must NOT click those!
      They have aria-label "Invite <other person> to connect" but are far down the page.
    """
    # Try 1: Direct Connect button using aria-label (most reliable)
    # Only click if it's in the top profile actions area (top < 600px)
    found = driver.execute_script("""
        const buttons = document.querySelectorAll(
            'button[aria-label*="Invite"][aria-label*="to connect"]'
        );
        for (const btn of buttons) {
            if (btn.offsetParent !== null) {
                const rect = btn.getBoundingClientRect();
                // Only click buttons in the profile header area, not "People also viewed"
                if (rect.top > 0 && rect.top < 600) {
                    btn.click();
                    return 'direct';
                }
            }
        }
        return null;
    """)

    if found:
        logger.debug("  Clicked Connect button directly on profile")
        return True

    # Try 2: Open "More actions" dropdown, then find Connect inside
    more_clicked = driver.execute_script("""
        // LinkedIn has two sets of profile action buttons (sticky header + main).
        // Match either aria-label variant.
        const buttons = document.querySelectorAll(
            'button[aria-label="More actions"], button[aria-label="More"]'
        );
        for (const btn of buttons) {
            if (btn.offsetParent !== null) {
                const rect = btn.getBoundingClientRect();
                if (rect.top > 0 && rect.top < 600) {
                    btn.click();
                    return true;
                }
            }
        }
        return false;
    """)

    if not more_clicked:
        logger.debug("  No Connect button and no More dropdown found")
        return False

    # Wait for dropdown to render (LinkedIn animates it in)
    import time
    for _wait in range(5):
        time.sleep(0.4)
        found_in_menu = driver.execute_script("""
            // Method 1: artdeco-dropdown__item with aria-label (most reliable)
            const items1 = document.querySelectorAll(
                '.artdeco-dropdown__item[aria-label*="Invite"][aria-label*="to connect"]'
            );
            for (const item of items1) {
                if (item.offsetParent !== null) {
                    item.click();
                    return 'dropdown-item';
                }
            }

            // Method 2: div[role="button"] with aria-label
            const items2 = document.querySelectorAll(
                'div[role="button"][aria-label*="Invite"][aria-label*="to connect"]'
            );
            for (const item of items2) {
                if (item.offsetParent !== null) {
                    item.click();
                    return 'role-button';
                }
            }

            // Method 3: Any visible dropdown li whose text is exactly "Connect"
            const lis = document.querySelectorAll(
                'div.artdeco-dropdown__content li, ul[role="menu"] li'
            );
            for (const li of lis) {
                const text = li.textContent.trim();
                if (text === 'Connect' && li.offsetParent !== null) {
                    // Click the inner div/button, not the li itself
                    const inner = li.querySelector('div[role="button"], button');
                    if (inner) { inner.click(); return 'li-inner'; }
                    li.click();
                    return 'li-text';
                }
            }

            return null;
        """)
        if found_in_menu:
            break

    if found_in_menu:
        logger.debug(f"  Clicked Connect inside More dropdown (via {found_in_menu})")
        return True

    logger.debug("  Connect not found in More dropdown either")
    return False


def _click_send_button(driver: webdriver.Chrome) -> bool:
    """Find and click the Send / Send invitation button in the connection modal."""
    import time

    human_delay(0.5, 1)

    # First, log what buttons exist in the modal so we can debug
    debug_info = driver.execute_script("""
        const modal = document.querySelector(
            'div.artdeco-modal, div.send-invite, div[role="dialog"]'
        );
        const scope = modal || document;
        const btns = scope.querySelectorAll('button');
        const info = [];
        for (const btn of btns) {
            if (btn.offsetParent !== null) {
                info.push({
                    text: btn.textContent.trim().substring(0, 60),
                    label: btn.getAttribute('aria-label') || '',
                    disabled: btn.disabled,
                    classes: btn.className.substring(0, 80)
                });
            }
        }
        return JSON.stringify(info);
    """)
    logger.debug(f"  Modal buttons: {debug_info}")

    # Wait up to 3 seconds for Send button to become enabled
    # (LinkedIn sometimes keeps it disabled briefly after typing)
    send_btn = None
    for attempt in range(6):
        send_btn = driver.execute_script("""
            const buttons = document.querySelectorAll('button');

            // Priority 1: aria-label match
            for (const btn of buttons) {
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if ((label.includes('send invitation') || label.includes('send now'))
                    && btn.offsetParent !== null && !btn.disabled) {
                    return btn;
                }
            }

            // Priority 2: exact text match on innerText (ignores child spans)
            for (const btn of buttons) {
                const text = btn.innerText.trim().toLowerCase();
                if ((text === 'send' || text === 'send invitation'
                     || text === 'send now')
                    && btn.offsetParent !== null && !btn.disabled) {
                    return btn;
                }
            }

            // Priority 3: partial text match (e.g., "Send" inside longer text)
            for (const btn of buttons) {
                const text = btn.innerText.trim().toLowerCase();
                if (text.startsWith('send') && !text.includes('without')
                    && btn.offsetParent !== null && !btn.disabled) {
                    return btn;
                }
            }

            return null;
        """)
        if send_btn:
            break
        time.sleep(0.5)

    if not send_btn:
        # Last resort: find even if disabled, and click anyway
        send_btn = driver.execute_script("""
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                const text = btn.innerText.trim().toLowerCase();
                if ((label.includes('send invitation') || label.includes('send now')
                     || text === 'send' || text === 'send invitation')
                    && btn.offsetParent !== null) {
                    return btn;
                }
            }
            return null;
        """)

    if not send_btn:
        logger.debug("  Could not find Send button at all")
        return False

    # Try ActionChains mouse-move + click first (most human-like)
    from utils import human_move_and_click
    if human_move_and_click(driver, send_btn):
        logger.debug("  Clicked Send via ActionChains mouse-move + click")
        human_delay(2, 3)
        return True

    # Fallback: Selenium click
    try:
        send_btn.click()
        logger.debug("  Clicked Send via Selenium .click()")
        human_delay(2, 3)
        return True
    except Exception as e:
        logger.debug(f"  Selenium click failed: {e}, trying JS click")

    # Fallback: JS click
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", send_btn)
        logger.debug("  Clicked Send via JS click")
        human_delay(2, 3)
        return True
    except Exception as e:
        logger.debug(f"  JS click also failed: {e}")
        return False


def _type_message(element, text: str):
    """Type a message character by character (human-like).
    
    Delegates to antidetect.realistic_typing which adds:
      - Variable per-character speed based on key position
      - Thinking pauses between words
      - Rare typo + backspace correction
      - Session fatigue scaling
    """
    realistic_typing(element, text)


def _simulate_profile_reading(driver: webdriver.Chrome):
    """Simulate a human reading a LinkedIn profile before taking action.

    Delegates to antidetect.realistic_profile_reading which uses:
      - Content-length-scaled reading time
      - Bézier curve mouse movements
      - Multiple scroll-stop-read cycles
      - Occasional scroll-back (re-reading)
    """
    realistic_profile_reading(driver)

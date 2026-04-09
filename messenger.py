"""
Messenger — finds employees at target companies on LinkedIn
and sends referral request messages.
"""

import hashlib
import json
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
from email_outreach import discover_email
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

    weekly_limit_hit = False
    for job in jobs:
        if weekly_limit_hit:
            break
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
        for contact in contacts:
            if total_sent >= Config.MAX_MESSAGES_PER_DAY:
                break
            if (weekly_connections + connections_today) >= Config.MAX_CONNECTIONS_PER_WEEK:
                break
            if sent_at_company >= Config.MAX_MESSAGES_PER_COMPANY:
                logger.debug(f"  → Hit per-company limit ({Config.MAX_MESSAGES_PER_COMPANY}) for {company}, moving on.")
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
            if result == "weekly_limit":
                logger.critical("🛑 Weekly invitation limit hit — stopping all outreach!")
                weekly_limit_hit = True
                break
            if result == "contract_skip":
                logger.debug(f"  → {contact.name} is contract, trying next contact")
                continue
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
                logger.info(f"  ✉️  Sent referral request to {contact.name} ({company})")

                # ── Email discovery (for next-day follow-up) ─────────
                if Config.EMAIL_ENABLED:
                    try:
                        db.set_contact_job_id(contact.contact_id, job.job_id)
                        email = discover_email(contact)
                        if email:
                            db.set_contact_email(contact.contact_id, email)
                            logger.debug(f"  📧 Discovered email: {email}")
                    except Exception as e:
                        logger.debug(f"  Email discovery failed: {e}")

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
                    weekly_limit_hit = True  # reuse flag to break outer loop too
                    break
            else:
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


def _scroll_profile_main(driver: webdriver.Chrome):
    """Scroll the LinkedIn ``<main>`` container to force-load all sections.

    LinkedIn renders profiles inside ``<main id="workspace">`` with
    ``overflow: scroll``.  ``window.scrollTo`` does nothing — we must
    scroll this container directly.  Each step pauses briefly so
    lazy-loaded sections (Experience, Education, …) actually render.
    """
    import time as _t
    try:
        scroll_height = driver.execute_script("""
            const m = document.querySelector('main#workspace, main');
            if (!m) return 0;
            return m.scrollHeight;
        """) or 0
        if scroll_height < 200:
            return  # no scrollable main container

        # Scroll down in steps
        for y in range(0, scroll_height, 400):
            driver.execute_script(f"""
                const m = document.querySelector('main#workspace, main');
                if (m) m.scrollTop = {y};
            """)
            _t.sleep(0.3)

        _t.sleep(0.5)  # let last lazy-loaded content render

        # Scroll back to top
        driver.execute_script("""
            const m = document.querySelector('main#workspace, main');
            if (m) m.scrollTop = 0;
        """)
        _t.sleep(0.3)
    except Exception:
        pass


def _is_contract_employee(driver: webdriver.Chrome) -> bool:
    """Check if the current profile's FIRST experience entry is contract/non-FT.

    LinkedIn renders profiles inside ``<main id="workspace">`` with
    ``overflow: scroll``.  We must scroll **that** container (not the
    window) to force-load the Experience section, then inspect the
    first company block for employment-type text like
    ``"Contract Full-time · 2 yrs"`` or ``"Part-time · 6 mos"``.
    """
    try:
        # Step 1: scroll the main container to load Experience section
        _scroll_profile_main(driver)

        # Step 2: detect employment type via JS
        employment_type = driver.execute_script(r"""
            // ── Find the Experience heading ─────────────────────
            let expHeading = null;
            for (const h of document.querySelectorAll('h2, h3')) {
                if (/^\s*Experience\s*$/i.test(h.textContent)) {
                    expHeading = h;
                    break;
                }
            }
            if (!expHeading) return null;  // no experience section

            // Walk up to the wrapping section / ancestor container.
            let expSection = expHeading.closest('section')
                             || expHeading.parentElement?.parentElement
                             || expHeading.parentElement;

            // ── Inspect the FIRST experience block ──────────────
            // The employment type line looks like:
            //   "Contract Full-time · 2 yrs"   ← contract
            //   "Full-time · 1 yr 3 mos"       ← fine
            //   "Part-time · 6 mos"            ← flagged
            //   "Internship · 3 mos"           ← flagged
            const headRect = expHeading.getBoundingClientRect();
            const els = expSection.querySelectorAll('p, span, div');
            for (const el of els) {
                const text = (el.innerText || el.textContent || '').trim();
                if (!text || text.length > 80) continue;
                if (el.children && el.children.length > 4) continue;

                const rect = el.getBoundingClientRect();
                // Only look within ~250px below the heading (first role)
                const dist = rect.top - headRect.top;
                if (dist < 0 || dist > 250) continue;

                const lower = text.toLowerCase();
                if (/\bcontract\b/i.test(lower)) return text;
                if (/\b(freelance|part[- ]?time|internship|temporary|temp)\b/i.test(lower)
                    && !/\bfull[- ]?time\b/i.test(lower)) return text;
            }
            return null;
        """)
        if employment_type:
            logger.debug(f"  📋 Detected non-FT employment: '{employment_type}'")
            return True
    except Exception as e:
        logger.debug(f"  Contract check failed (proceeding anyway): {e}")
    return False


def _send_connection_with_note(
    driver: webdriver.Chrome,
    contact: Contact,
    message: str,
) -> str:
    """
    Navigate to the contact's profile and send a Connect request
    with a personalized note, or a DM if already connected.
    Returns: 'connection_sent', 'dm_sent', 'contract_skip', or 'failed'.
    """
    try:
        if not safe_get(driver, contact.profile_url):
            return "failed"

        # ── Anti-detection: realistic profile reading ─────────────
        # Uses content-scaled timing, Bézier mouse, scroll-back patterns
        get_session().record_profile_view()
        realistic_profile_reading(driver)

        # ── Skip contract / non-FT employees ─────────────────────
        if _is_contract_employee(driver):
            logger.info(f"  📋 Skipping {contact.name} — contract/non-FT employee")
            return "contract_skip"

        # _get_connection_status scrolls to top internally
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
        clicked = _click_connect_button(driver, expected_name=contact.name)
        if not clicked:
            logger.debug(f"  No Connect button for {contact.name}; trying direct Message fallback")
            return "dm_sent" if _send_direct_message(driver, contact, message) else "failed"

        human_delay(0.5, 1)

        # Some LinkedIn layouts require a second click on a visible
        # Connect option in a popover/menu after the first click.
        if _click_secondary_connect_option(driver):
            logger.debug("  Clicked secondary Connect option after initial click")
            human_delay(0.5, 1)

        # Close any leftover More dropdown that might overlap with the modal
        try:
            driver.execute_script("""
                // Press Escape to close any open dropdown/popover
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                // Also try clicking outside the dropdown
                const dropdowns = document.querySelectorAll(
                    'div.artdeco-dropdown__content--is-open, [role="menu"]'
                );
                for (const dd of dropdowns) {
                    if (dd && dd.offsetParent !== null) {
                        const toggle = dd.previousElementSibling;
                        if (toggle && toggle.getAttribute('aria-expanded') === 'true') {
                            toggle.click();
                        }
                    }
                }
            """)
        except Exception:
            pass

        # DO NOT check _is_invite_pending here — it can false-match "Pending"
        # text on the profile page (e.g., "Connect if you know each other → Pending").
        # Instead, we proceed to look for the "Add a note" modal first.
        # _is_invite_pending is only checked later as a last resort.

        # Step 2: Wait for modal to appear and look for "Add a note" button
        import time as _time
        from selenium.webdriver.common.action_chains import ActionChains as _AC
        from selenium.webdriver.common.by import By as _By

        # Give the modal time to animate in
        _time.sleep(1.2)

        # LinkedIn 2026 renders the invitation modal inside a Shadow DOM
        # (host: div.theme--light).  Regular querySelectorAll and Selenium
        # XPath cannot pierce Shadow DOM, so we must use JS to access
        # shadowRoot and find elements inside it.

        # Helper: find shadow root containing the modal
        def _find_shadow_modal_btn(driver, text_match):
            """Search all shadow roots for a button whose text includes text_match."""
            return driver.execute_script("""
                const target = arguments[0].toLowerCase();
                // Find all shadow hosts
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    if (!el.shadowRoot) continue;
                    const btns = el.shadowRoot.querySelectorAll('button, a, [role="button"]');
                    for (const btn of btns) {
                        const r = btn.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                        const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (text.includes(target) || label.includes(target)) {
                            return btn;
                        }
                    }
                    // Also recurse into nested shadow roots
                    const nested = el.shadowRoot.querySelectorAll('*');
                    for (const nel of nested) {
                        if (!nel.shadowRoot) continue;
                        const nbtns = nel.shadowRoot.querySelectorAll('button, a, [role="button"]');
                        for (const btn of nbtns) {
                            const r = btn.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) continue;
                            const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
                            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                            if (text.includes(target) || label.includes(target)) {
                                return btn;
                            }
                        }
                    }
                }
                return null;
            """, text_match)

        def _find_shadow_textarea(driver):
            """Search all shadow roots for a textarea or contenteditable."""
            return driver.execute_script("""
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    if (!el.shadowRoot) continue;
                    // Only look in shadow roots that contain invitation-related text
                    // (skip the messaging overlay widget)
                    const rootText = (el.shadowRoot.textContent || '').toLowerCase();
                    if (!rootText.includes('add a note') && !rootText.includes('invitation')
                        && !rootText.includes('how do you know') && !rootText.includes('connect')) continue;
                    // textarea
                    const tas = el.shadowRoot.querySelectorAll('textarea');
                    for (const ta of tas) {
                        const r = ta.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return ta;
                    }
                    // contenteditable
                    const ces = el.shadowRoot.querySelectorAll('[contenteditable="true"]');
                    for (const ce of ces) {
                        const r = ce.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) return ce;
                    }
                }
                return null;
            """)

        # Debug: check if shadow modal exists
        shadow_debug = driver.execute_script("""
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                if (!el.shadowRoot) continue;
                const text = (el.shadowRoot.textContent || '').toLowerCase();
                if (text.includes('add a note') || text.includes('invitation')) {
                    const btns = el.shadowRoot.querySelectorAll('button, a');
                    const btnTexts = [];
                    for (const b of btns) {
                        const t = (b.innerText || '').trim();
                        if (t) btnTexts.push(t);
                    }
                    return JSON.stringify({shadowHost: el.tagName + '.' + (el.className||'').substring(0,30), buttons: btnTexts});
                }
            }
            return '{"shadowHost":"none","buttons":[]}';
        """)
        logger.debug(f"  Shadow modal check: {shadow_debug}")

        # Step 2: Find and click "Add a note" button
        add_note_btn = None
        for attempt in range(5):
            # Try shadow DOM first (LinkedIn 2026 standard)
            add_note_btn = _find_shadow_modal_btn(driver, "add a note")
            if add_note_btn:
                logger.debug(f"  Found 'Add a note' in shadow DOM on attempt {attempt + 1}")
                break

            # Fallback: try regular DOM (older LinkedIn layouts)
            try:
                candidates = driver.find_elements(
                    _By.XPATH,
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'add a note')]"
                    " | //a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'add a note')]"
                )
                for btn in candidates:
                    if btn.is_displayed() and btn.is_enabled():
                        add_note_btn = btn
                        logger.debug(f"  Found 'Add a note' in regular DOM on attempt {attempt + 1}")
                        break
            except Exception:
                pass

            if add_note_btn:
                break
            _time.sleep(0.5)

        if add_note_btn:
            # JS click works reliably for both shadow DOM and regular DOM elements
            try:
                driver.execute_script("arguments[0].click();", add_note_btn)
                logger.debug("  Clicked 'Add a note' via JS click")
            except Exception:
                try:
                    _AC(driver).move_to_element(add_note_btn).pause(0.15).click().perform()
                    logger.debug("  Clicked 'Add a note' via ActionChains")
                except Exception as e:
                    logger.debug(f"  Failed to click 'Add a note': {e}")
            human_delay(0.5, 1)
        else:
            logger.debug("  No 'Add a note' button — may already show textarea")

        # Step 3: Find the textarea and type the message
        # Search shadow DOM first, then regular DOM.
        note_field = None
        for _ in range(5):
            # Shadow DOM textarea/contenteditable
            note_field = _find_shadow_textarea(driver)
            if note_field:
                logger.debug(f"  Found note input in shadow DOM")
                break

            # Regular DOM fallback
            textarea_selectors = [
                "textarea[name='message']",
                "textarea#custom-message",
                "textarea.connect-button-send-invite__custom-message",
                "div[role='dialog'] textarea",
                "div.artdeco-modal textarea",
                "textarea",
            ]
            for sel in textarea_selectors:
                try:
                    candidates = driver.find_elements(_By.CSS_SELECTOR, sel)
                    for ta in candidates:
                        if ta.is_displayed():
                            note_field = ta
                            break
                except Exception:
                    pass
                if note_field:
                    break

            if not note_field:
                ce_selectors = [
                    "div[role='dialog'] [contenteditable='true']",
                    "div.artdeco-modal [contenteditable='true']",
                ]
                for sel in ce_selectors:
                    try:
                        candidates = driver.find_elements(_By.CSS_SELECTOR, sel)
                        for ce in candidates:
                            if ce.is_displayed():
                                note_field = ce
                                break
                    except Exception:
                        pass
                    if note_field:
                        break

            if note_field:
                logger.debug(f"  Found note input field: {note_field.tag_name}")
                break
            _time.sleep(0.5)

        if not note_field:
                logger.debug("  No note input found — trying send without note")
                if _click_send_button(driver):
                    return "connection_sent"
                if _is_invite_pending(driver):
                    logger.debug("  No send button, but Pending state is visible — treating as sent")
                    return "connection_sent"
                logger.debug("  Connect-note flow failed; trying direct Message fallback")
                return "dm_sent" if _send_direct_message(driver, contact, message) else "failed"

        # Click into the field first (important for shadow DOM elements)
        try:
            driver.execute_script("arguments[0].focus(); arguments[0].click();", note_field)
        except Exception:
            pass

        try:
            note_field.clear()
        except Exception:
            try:
                driver.execute_script("arguments[0].textContent=''; arguments[0].value='';", note_field)
            except Exception:
                pass

        _type_message(note_field, message[:300])
        human_delay(0.5, 1)

        # Step 4: Click Send
        if _click_send_button(driver):
            # Check for weekly invitation limit message
            if _check_weekly_limit_message(driver):
                logger.critical("🛑 LinkedIn weekly invitation limit reached!")
                return "weekly_limit"
            return "connection_sent"
        if _is_invite_pending(driver):
            logger.debug("  Send button not found, but Pending state detected after note flow")
            return "connection_sent"
        logger.debug("  Send button still missing; trying direct Message fallback")
        return "dm_sent" if _send_direct_message(driver, contact, message) else "failed"

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
        # Scroll to top so action buttons are visible
        try:
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.3)
        except Exception:
            pass

        # Click the "Message" button on the profile page
        # LinkedIn 2026 renders Message as <a>, not <button>
        msg_clicked = driver.execute_script("""
            const controls = document.querySelectorAll(
                'button, a, [role="button"]'
            );
            let best = null;
            let bestTop = 999999;
            for (const ctrl of controls) {
                if (!ctrl) continue;
                const rect = ctrl.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;
                if (ctrl.closest('aside, nav, header[role="banner"]')) continue;

                const label = (ctrl.getAttribute('aria-label') || '').toLowerCase();
                const text = (ctrl.innerText || ctrl.textContent || '').trim().toLowerCase();
                const isMessage = label.startsWith('message ') || label === 'message'
                    || text === 'message' || text.includes('message');
                if (!isMessage) continue;
                // Avoid matching "Message top connections" or similar longer text
                if (text.length > 20) continue;

                if (!(rect.top > -10 && rect.top < 650)) continue;
                if (rect.left >= window.innerWidth * 0.72) continue;

                if (rect.top < bestTop) {
                    bestTop = rect.top;
                    best = ctrl;
                }
            }
            if (best) {
                best.click();
                return true;
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

    Strategy: scroll to top first, then scan <button> elements + elements
    with role="button" using flexible text matching (includes, not exact).
    """
    # Scroll to top so action buttons are in viewport
    try:
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass
    import time as _t
    _t.sleep(0.5)

    result = driver.execute_script("""
        // Phase 1: Scan <button>, <a>, and [role="button"] elements in the
        // profile action area.  LinkedIn 2026 renders Connect/Message as <a>
        // links styled as buttons, not actual <button> elements.
        const candidates = document.querySelectorAll(
            'button, a, [role="button"]'
        );
        let hasConnectDirect = false;
        let hasMessage = false;
        let hasPending = false;
        let hasFollow = false;
        const debugBtns = [];

        for (const el of candidates) {
            if (!el || el.offsetParent === null) continue;

            // Skip sidebar, nav, banner
            if (el.closest('aside')) continue;
            if (el.closest('nav')) continue;
            if (el.closest('header[role="banner"]')) continue;

            const rect = el.getBoundingClientRect();
            // Profile action buttons are in the top portion of the page
            if (rect.top < -10 || rect.top >= 650) continue;
            // Exclude far-right sidebar elements
            if (rect.left >= window.innerWidth * 0.72) continue;

            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            const blob = text + ' ' + label;

            // Collect debug info for first 15 buttons
            if (debugBtns.length < 15) {
                debugBtns.push(text.substring(0, 30) + '|' + label.substring(0, 40)
                    + '|t=' + Math.round(rect.top) + ',l=' + Math.round(rect.left));
            }

            // PENDING detection
            if (text.includes('pending') || label.includes('pending')
                || label.includes('withdraw invitation') || label.includes('invitation sent')) {
                hasPending = true;
            }

            // CONNECT detection (handles "Connect", "+ Connect", icon text + "Connect", etc.)
            if ((text.includes('connect') && !text.includes('disconnect')
                 && !text.includes('connections') && !text.includes('connected'))
                || (label.includes('connect') && label.includes('invite')
                    && !label.includes('disconnect'))) {
                hasConnectDirect = true;
            }

            // MESSAGE detection (handles "Message", icon + "Message", etc.)
            if (text.includes('message') || label.startsWith('message ')
                || label === 'message') {
                hasMessage = true;
            }

            // FOLLOW detection (handles "Follow", "+ Follow", etc.)
            // If we see Follow in the action area and no Connect, it's a follow-first layout
            if ((text.includes('follow') && !text.includes('unfollow')
                 && !text.includes('follower'))
                || (label.startsWith('follow ') && !label.includes('unfollow'))) {
                hasFollow = true;
            }
        }

        // Phase 2: If nothing found via buttons, scan leaf text nodes
        // as a fallback (LinkedIn may use custom non-button elements).
        if (!hasConnectDirect && !hasMessage && !hasPending && !hasFollow) {
            const allElements = document.querySelectorAll('*');
            for (const el of allElements) {
                if (!el || el.offsetParent === null) continue;
                if (el.children && el.children.length > 2) continue;  // prefer leaf-ish nodes
                if (el.closest('aside, nav, header[role="banner"]')) continue;

                const rect = el.getBoundingClientRect();
                if (rect.top < -10 || rect.top >= 650) continue;
                if (rect.left >= window.innerWidth * 0.72) continue;

                const text = (el.innerText || el.textContent || '').trim();
                if (text.length > 40) continue;
                const textLower = text.toLowerCase();
                const label = (el.getAttribute('aria-label') || '').toLowerCase();

                if (textLower === 'connect'
                    || (label.includes('invite') && label.includes('connect')
                        && !label.includes('disconnect'))) {
                    hasConnectDirect = true;
                }
                if (textLower === 'message' || label.startsWith('message ')) {
                    hasMessage = true;
                }
                if (textLower === 'pending' || label.includes('pending')
                    || label.includes('withdraw invitation')) {
                    hasPending = true;
                }
                if ((textLower === 'follow' || label.startsWith('follow '))
                    && !textLower.includes('unfollow')) {
                    hasFollow = true;
                }
            }
        }

        let status = 'unknown';
        if (hasPending) status = 'pending';
        else if (hasConnectDirect) status = 'connect';
        else if (hasFollow && hasMessage) status = 'connect';  // Connect is inside "More"
        else if (hasFollow && !hasMessage) status = 'connect';  // follow-first, Connect in More
        else if (hasMessage && !hasConnectDirect && !hasFollow) status = 'connected';

        return JSON.stringify({
            status: status,
            flags: {connect: hasConnectDirect, message: hasMessage, pending: hasPending, follow: hasFollow},
            buttons: debugBtns
        });
    """) or '{"status":"unknown","flags":{},"buttons":[]}'

    try:
        data = json.loads(result)
        status = data.get("status", "unknown")
        flags = data.get("flags", {})
        btns = data.get("buttons", [])
        logger.debug(
            f"  Connection status: {status}  "
            f"(flags: C={flags.get('connect')}, M={flags.get('message')}, "
            f"P={flags.get('pending')}, F={flags.get('follow')})  "
            f"buttons=[{', '.join(btns[:8])}]"
        )
    except (json.JSONDecodeError, AttributeError):
        status = "unknown"
        logger.debug(f"  Connection status: {status} (raw: {result!r})")
    return status


def _click_connect_button(driver: webdriver.Chrome, expected_name: str = "") -> bool:
    """
    Find and click Connect on a profile.

    Two strategies (tried in order):
      1. Direct blue Connect button next to Message (when visible)
      2. More → Connect dropdown option (follow-first layouts)

    Uses Selenium native find_elements for reliability — JS innerText misses
    buttons with visually-hidden text spans (common on LinkedIn 2026).
    """
    import time
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By as _By

    # Scroll to top so buttons are in viewport
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
    except Exception:
        pass

    # ── Step 1: Try direct Connect button ────────────────────────────
    # LinkedIn 2026 renders Connect as <a>, <button>, or [role="button"].
    # Search all three element types.
    direct_btn = None
    try:
        candidates = driver.find_elements(
            _By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
            " | //a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
            " | //*[@role='button'][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
        )
        vw = driver.execute_script("return window.innerWidth;")
        logger.debug(f"  Direct Connect XPath found {len(candidates)} candidates")
        for idx, btn in enumerate(candidates[:8]):
            try:
                _t = (btn.text or "").strip()[:30]
                _l = (btn.get_attribute("aria-label") or "")[:30]
                _d = btn.is_displayed()
                _loc = btn.location
                _sz = btn.size
                logger.debug(f"    candidate[{idx}]: tag={btn.tag_name} text='{_t}' label='{_l}' displayed={_d} loc={_loc} size={_sz}")
            except Exception:
                pass
        for btn in candidates:
            if not btn.is_displayed():
                continue
            text = (btn.text or "").strip().lower()
            label = (btn.get_attribute("aria-label") or "").lower()
            # Skip negative matches
            if any(x in text for x in ["disconnect", "connections", "connected"]):
                continue
            if any(x in label for x in ["disconnect", "connections", "connected"]):
                continue
            # Must be a real Connect button, not a "mutual connection" link.
            # Real buttons: text="Connect" (short), label="Invite X to connect"
            # False positives: "Neel, Ihor and 1 other mutual connection" (long)
            if len(text) > 15 and "invite" not in label:
                continue
            # Skip sidebar buttons (right column "More profiles for you")
            loc = btn.location
            size = btn.size
            if loc.get("x", 0) + size.get("width", 0) / 2 >= vw * 0.72:
                continue
            # Skip dropdown/menu items
            try:
                parent_classes = driver.execute_script(
                    "return arguments[0].closest("
                    "'div.artdeco-dropdown__content, [role=\"menu\"],"
                    " .artdeco-popover__content') !== null;", btn)
                if parent_classes:
                    continue
            except Exception:
                pass
            # Must be in the upper portion of the page
            if loc.get("y", 0) > 650:
                continue
            direct_btn = btn
            logger.debug(f"  Found direct Connect button: text='{text}', label='{label}', "
                         f"y={loc.get('y')}, x={loc.get('x')}")
            break
    except Exception as e:
        logger.debug(f"  Error searching for direct Connect: {e}")

    if direct_btn:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", direct_btn)
            time.sleep(0.2)
            ActionChains(driver).move_to_element(direct_btn).pause(0.15).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click();", direct_btn)
        logger.debug("  Clicked direct Connect button via ActionChains")
        return True

    # ── Step 2: No direct Connect — try More → Connect ───────────────
    more_btn = None
    try:
        # Find the profile-section More button (not the navbar one)
        candidates = driver.find_elements(
            _By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'more')]"
            " | //button[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'more actions')]"
        )
        for btn in candidates:
            if not btn.is_displayed():
                continue
            text = (btn.text or "").strip().lower()
            label = (btn.get_attribute("aria-label") or "").lower()
            # Skip "Learn more", "Show more", etc.
            if any(x in text for x in ["learn", "show", "see", "load"]):
                continue
            # Must be "more" or "more actions" specifically
            if text not in ("more", "") and label not in ("more actions", "more", "more options"):
                continue
            loc = btn.location
            if loc.get("y", 0) > 650 or loc.get("y", 0) < 0:
                continue
            vw = driver.execute_script("return window.innerWidth;")
            if loc.get("x", 0) >= vw * 0.72:
                continue
            more_btn = btn
            break
    except Exception as e:
        logger.debug(f"  Error searching for More button: {e}")

    if more_btn:
        # Click More with real mouse events
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", more_btn)
            time.sleep(0.2)
            ActionChains(driver).move_to_element(more_btn).pause(0.15).click().perform()
            logger.debug("  Clicked More actions button via ActionChains")
        except Exception as e:
            logger.debug(f"  ActionChains click on More failed: {e}, trying JS")
            driver.execute_script("arguments[0].click();", more_btn)

        # Wait for dropdown and find Connect option using Selenium native
        connect_in_menu = None
        for attempt in range(12):
            time.sleep(0.35)
            try:
                # Search for Connect-like items in the dropdown
                menu_items = driver.find_elements(
                    _By.XPATH,
                    "//*[@role='menu']//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
                    " | //*[@role='menu']//*[@role='menuitem'][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
                    " | //*[contains(@class,'artdeco-dropdown')]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
                    " | //*[contains(@class,'artdeco-dropdown')]//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
                    " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
                )
                for item in menu_items:
                    if not item.is_displayed():
                        continue
                    text = (item.text or "").strip().lower()
                    label = (item.get_attribute("aria-label") or "").lower()
                    # Skip destructive actions
                    if any(x in text + label for x in [
                        "disconnect", "connections", "connected",
                        "remove", "block", "report", "unfollow"
                    ]):
                        continue
                    connect_in_menu = item
                    break
            except Exception:
                pass

            if connect_in_menu:
                try:
                    ActionChains(driver).move_to_element(connect_in_menu).pause(0.15).click().perform()
                except Exception:
                    driver.execute_script("arguments[0].click();", connect_in_menu)
                logger.debug("  Clicked Connect inside More dropdown via ActionChains")
                return True

        logger.debug("  More button found but no Connect option in dropdown")
        # Close the dropdown before giving up
        try:
            driver.execute_script("""
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
            """)
            time.sleep(0.3)
        except Exception:
            pass

    logger.debug("  Connect not found via direct button or More->Connect")
    return False


def _click_send_button(driver: webdriver.Chrome) -> bool:
    """Find and click the Send / Send invitation button in the connection modal."""
    import time
    from selenium.webdriver.common.action_chains import ActionChains as _AC
    from selenium.webdriver.common.by import By as _By

    human_delay(0.5, 1)

    send_texts = [
        "send",
        "send invitation",
        "send now",
        "send without a note",
        "send without note",
    ]

    send_btn = None

    for attempt in range(5):
        # --- Shadow DOM search first ---
        send_btn = driver.execute_script("""
            const targets = arguments[0];
            const allEls = document.querySelectorAll('*');
            let best = null, bestScore = -999;
            for (const el of allEls) {
                if (!el.shadowRoot) continue;
                const btns = el.shadowRoot.querySelectorAll('button, a, [role="button"]');
                for (const btn of btns) {
                    const r = btn.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const text = (btn.innerText || '').trim().toLowerCase();
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const blob = label + ' ' + text;
                    let isSend = false;
                    for (const t of targets) {
                        if (text === t || label.startsWith(t) || blob.includes(t)) { isSend = true; break; }
                    }
                    if (!isSend) continue;
                    let score = 0;
                    const cls = (btn.getAttribute('class') || '').toLowerCase();
                    if (cls.includes('primary')) score += 4;
                    if (blob.includes('send invitation')) score += 4;
                    else if (blob.includes('send now')) score += 3;
                    else if (text === 'send') score += 2;
                    if (r.y > 80) score += 2;
                    if (score > bestScore) { bestScore = score; best = btn; }
                }
            }
            return best;
        """, send_texts)
        if send_btn:
            logger.debug(f"  Found Send button in shadow DOM on attempt {attempt + 1}")
            break

        # --- Regular DOM fallback ---
        try:
            all_buttons = driver.find_elements(
                _By.XPATH, "//button | //a | //*[@role='button']"
            )
            best = None
            best_score = -999
            for btn in all_buttons:
                if not btn.is_displayed() or not btn.is_enabled():
                    continue
                text = (btn.text or "").strip().lower()
                label = (btn.get_attribute("aria-label") or "").lower()
                blob = f"{label} {text}"

                is_send = any(
                    text == t or label.startswith(t) or t in blob
                    for t in send_texts
                )
                if not is_send:
                    continue

                score = 0
                cls = (btn.get_attribute("class") or "").lower()
                if "artdeco-button--primary" in cls:
                    score += 4
                if "send invitation" in blob:
                    score += 4
                elif "send now" in blob:
                    score += 3
                elif text == "send":
                    score += 2

                loc = btn.location
                if loc.get("y", 0) > 80:
                    score += 2

                if score > best_score:
                    best_score = score
                    best = btn

            if best and best_score >= 2:
                send_btn = best
                logger.debug(f"  Found Send button in regular DOM on attempt {attempt + 1}")
                break
        except Exception:
            pass

        time.sleep(0.4)

    if not send_btn:
        logger.debug("  Could not find Send button at all")
        return False

    # Try JS click first for shadow DOM elements (ActionChains may not work)
    try:
        driver.execute_script("arguments[0].click();", send_btn)
        logger.debug("  Clicked Send via JS click")
        human_delay(2, 3)
        return True
    except Exception as e:
        logger.debug(f"  JS click failed: {e}")

    # Fallback: ActionChains
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
        logger.debug(f"  All click methods failed: {e}")
        return False


def _click_secondary_connect_option(driver: webdriver.Chrome) -> bool:
    """Click a visible secondary Connect option from an open popover/menu.

    Some profile UIs show an intermediate action sheet after the first Connect
    click. This helper clicks the final visible Connect item when present.
    Uses ActionChains for real mouse events.
    """
    from selenium.webdriver.common.action_chains import ActionChains as _AC
    try:
        elem = driver.execute_script("""
            const scopes = document.querySelectorAll(
                'div.artdeco-dropdown__content, [role="menu"], ul[role="menu"], '
                + '.artdeco-popover__content, div[role="dialog"]'
            );

            for (const scope of scopes) {
                if (!scope || scope.offsetParent === null) continue;
                const rect = scope.getBoundingClientRect();
                if (rect.top > window.innerHeight || rect.bottom < 0) continue;

                const entries = scope.querySelectorAll(
                    'button, div[role="button"], a[role="button"], span[role="button"], li, '
                    + 'div[role="menuitem"], a[role="menuitem"]'
                );

                for (const entry of entries) {
                    if (!entry || entry.offsetParent === null) continue;
                    const text = (entry.innerText || entry.textContent || '').trim().toLowerCase();
                    const label = (entry.getAttribute('aria-label') || '').toLowerCase();
                    const blob = `${label} ${text}`;

                    if (blob.includes('remove connection') || blob.includes('disconnect') || blob.includes('unfollow')) {
                        continue;
                    }

                    const isConnect = (
                        (label.includes('invite') && label.includes('connect'))
                        || text === 'connect'
                        || text.startsWith('connect')
                        || (text.includes('invite') && text.includes('connect'))
                    );
                    if (!isConnect) continue;

                    const clickable = entry.matches('button, div[role="button"], a[role="button"], span[role="button"]')
                        ? entry
                        : (entry.querySelector('button, div[role="button"], a[role="button"], span[role="button"]') || entry);
                    return clickable;
                }
            }
            return null;
        """)
        if elem:
            try:
                _AC(driver).move_to_element(elem).pause(0.15).click().perform()
            except Exception:
                driver.execute_script("arguments[0].click();", elem)
            return True
        return False
    except Exception:
        return False


def _check_weekly_limit_message(driver: webdriver.Chrome) -> bool:
    """Check if LinkedIn is showing the weekly connection invitation limit message.

    Searches both regular DOM and shadow DOM for text like:
    'reached the weekly limit for connection invitations'
    """
    try:
        return bool(driver.execute_script("""
            const needle = 'weekly limit';
            // Check regular DOM
            const body = (document.body.innerText || '').toLowerCase();
            if (body.includes(needle) && body.includes('invitation')) return true;
            // Check shadow DOM
            const allEls = document.querySelectorAll('*');
            for (const el of allEls) {
                if (!el.shadowRoot) continue;
                const text = (el.shadowRoot.textContent || '').toLowerCase();
                if (text.includes(needle) && text.includes('invitation')) return true;
            }
            return false;
        """))
    except Exception:
        return False


def _is_invite_pending(driver: webdriver.Chrome) -> bool:
    """Return True if profile UI indicates an invite was already sent/pending.
    
    IMPORTANT: Only checks for Pending as a PRIMARY action button in the 
    profile header area (where Connect button was). Does NOT match Pending
    text in dropdown menus, "Connect if you know each other" sections, or
    other parts of the page.
    """
    try:
        return bool(driver.execute_script("""
            // Only check primary action buttons in the profile header area
            // (typically top 500px, left 72% of viewport)
            const elems = document.querySelectorAll('button, [role="button"]');
            for (const el of elems) {
                if (!el || el.offsetParent === null) continue;
                if (el.closest('aside, nav')) continue;
                // Skip dropdown menus 
                if (el.closest('div.artdeco-dropdown__content, [role="menu"], ul[role="menu"], '
                    + '.artdeco-popover__content, div[class*="dropdown"]')) continue;
                // Skip "Connect if you know each other" section and similar
                if (el.closest('section, div[class*="pymk"], div[class*="highlight"]')) {
                    // But don't skip the main profile header section
                    const section = el.closest('section');
                    if (section) {
                        const headingText = (section.querySelector('h2, h3') || {}).textContent || '';
                        if (headingText.toLowerCase().includes('connect if')
                            || headingText.toLowerCase().includes('highlight')
                            || headingText.toLowerCase().includes('people')
                            || headingText.toLowerCase().includes('similar')) continue;
                    }
                }
                const rect = el.getBoundingClientRect();
                // Only check buttons in the profile header action area (narrow range)
                if (rect.top < -10 || rect.top >= 500) continue;
                if (rect.left >= window.innerWidth * 0.72) continue;

                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const text = (el.innerText || el.textContent || '').trim().toLowerCase();

                if (text === 'pending') return true;
                if (label.includes('withdraw invitation')) return true;
            }

            // Toast confirmation fallback
            const toasts = document.querySelectorAll('[role="alert"], .artdeco-toast-item, div[class*="toast"]');
            for (const toast of toasts) {
                if (!toast) continue;
                const t = (toast.innerText || toast.textContent || '').toLowerCase();
                if (t.includes('invitation sent') || t.includes('request sent')
                    || t.includes('invite sent') || t.includes('connection sent')) return true;
            }

            return false;
        """))
    except Exception:
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

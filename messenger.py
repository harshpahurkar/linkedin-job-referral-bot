"""
Messenger — finds employees at target companies on LinkedIn
and sends referral request messages.
"""

import hashlib
import random

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
            f"🛑 Weekly profile-view limit approaching "
            f"({weekly_profiles}/{Config.MAX_PROFILE_VIEWS_PER_WEEK}). "
            f"Skipping outreach to protect your account."
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
            logger.info("🛑 Would exceed weekly connection limit. Stopping.")
            break

        company = job.company.strip()
        if not company or company == "Unknown" or company in companies_processed:
            continue

        # ── Anti-detection: skip ~12% of companies randomly ─────────
        # Breaks the exhaustive crawl pattern that bots exhibit.
        if random.random() < 0.12:
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

            message = _pick_message(contact, job)

            result = _send_connection_with_note(driver, contact, message)
            if result in ("connection_sent", "dm_sent"):
                db.mark_messaged(contact.contact_id)
                db.mark_referral_requested(job.job_id)
                if result == "connection_sent":
                    db.log_activity("connection_request", contact.name)
                    connections_today += 1
                else:
                    db.log_activity("direct_message", contact.name)
                total_sent += 1
                sent_at_company += 1
                logger.info(f"  ✉️  Sent referral request to {contact.name} ({company})")
                long_delay()  # respect rate limits
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


def _pick_message(contact: Contact, job: Job) -> str:
    """
    Select and format the best message template for this contact + job.
    Strategy:
      1. If contact looks like a school alum → use the school template (#3)
      2. Otherwise, match the template angle to the JOB TITLE:
         - Full-stack / frontend / react roles → T1 (gov + full-stack)
         - Backend / microservices / API roles → T2 (microservices + CI/CD)
         - Cloud / DevOps / infra / platform / SRE roles → T4 (cloud breadth)
         - Automation / QA / SDET / testing roles → T1 (gov automation)
         - Anything else → T5 (short + punchy, covers everything)
      3. If no keyword matches, rotate through non-school templates
    All messages are capped at 300 chars (LinkedIn's hard limit).
    """
    global _msg_counter

    templates = Config.REFERRAL_TEMPLATES
    school_template_idx = 2  # the "fellow alum" template

    # Check if contact's profile hints at shared school
    contact_text = f"{contact.title} {contact.name}".lower()
    is_alum = Config.YOUR_SCHOOL and Config.YOUR_SCHOOL.lower() in contact_text

    if is_alum and school_template_idx < len(templates):
        template = templates[school_template_idx]
    else:
        # Match template to job description keywords
        jt = job.title.lower()

        # T1 (idx 0) — Gov + full-stack: full-stack, frontend, react, angular
        # T2 (idx 1) — Microservices + CI/CD: backend, api, microservice, java, spring
        # T3 (idx 2) — School alum (handled above)
        # T4 (idx 3) — Cloud breadth: cloud, devops, infra, platform, sre, kubernetes
        # T5 (idx 4) — Short punchy: default / catch-all

        if any(kw in jt for kw in [
            "full-stack", "full stack", "fullstack", "frontend",
            "front-end", "front end", "react", "angular", "vue",
        ]):
            template = templates[0]  # T1 — gov + full-stack apps
        elif any(kw in jt for kw in [
            "backend", "back-end", "back end", "microservice",
            "api", "java", "spring", "node", "express",
        ]):
            template = templates[1]  # T2 — microservices + CI/CD
        elif any(kw in jt for kw in [
            "cloud", "devops", "dev ops", "infrastructure", "infra",
            "platform", "sre", "site reliability", "kubernetes",
            "aws", "azure", "gcp",
        ]):
            template = templates[3]  # T4 — cloud breadth
        elif any(kw in jt for kw in [
            "automation", "qa", "quality", "sdet", "test",
            "selenium", "cypress",
        ]):
            template = templates[0]  # T1 — gov automation angle
        else:
            # No clear match — use the short punchy one or rotate
            non_school = [t for i, t in enumerate(templates) if i != school_template_idx]
            template = non_school[_msg_counter % len(non_school)]
            _msg_counter += 1

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
    )

    # Hard cap at 300 characters
    if len(msg) > 300:
        msg = msg[:297] + "..."

    logger.debug(
        f"  📝 Message for {contact.name}: role='{job_title}' "
        f"company='{job.company}' [{len(msg)} chars]"
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
    "canada", ", ca",
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


def _company_name_matches(expected: str, found: str) -> bool:
    """
    Check if a found company name is a reasonable match for the expected one.
    Handles cases like 'Unity' vs 'Unity Technologies' or 'IBM' vs 'IBM Canada'.
    """
    e = expected.lower().strip()
    f = found.lower().strip()
    if not e or not f:
        return False
    # Exact match
    if e == f:
        return True
    # One contains the other (e.g. "Unity" in "Unity Technologies")
    if e in f or f in e:
        return True
    # First word matches for short names (e.g. "IBM" == "IBM")
    if len(e) <= 5 and f.startswith(e):
        return True
    return False


def _browse_company_people_page(
    driver: webdriver.Chrome,
    db: Database,
    company: str,
    max_results: int = 8,
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
        driver.get(search_url)
        human_delay(1, 1.5)
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

            if results:
                # Find the best-matching company
                for r in results:
                    if _company_name_matches(company, r["name"]):
                        company_url = r["url"]
                        logger.debug(f"  ✅ Matched company: '{r['name']}' → {company_url}")
                        break
                if not company_url:
                    # No good match — log and skip
                    found_names = [r["name"] for r in results[:5]]
                    logger.warning(
                        f"  ⚠️  No company match for '{company}' in search results: "
                        f"{found_names}. Skipping."
                    )
                    return contacts
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
        # Add "Canada" as a keyword filter to get local employees.
        people_url = company_url.rstrip("/") + "/people/"
        job_location = (job.location if job else "").strip()
        if _is_canadian_location(job_location) or "canada" in Config.JOB_LOCATION.lower():
            people_url += "?keywords=Canada"

        driver.get(people_url)
        human_delay(1, 1.5)
        scroll_page(driver, scrolls=6)

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
            logger.debug(f"  No people found via JS on {company} people page")
            return contacts

        logger.debug(f"  Raw people found: {len(people_data)}")

        # Build all contacts first, then filter for relevant titles
        all_contacts: list[Contact] = []
        for person in people_data:
            name = person.get("name", "").strip()
            title_text = person.get("title", "").strip()
            profile_url = person.get("link", "").strip()
            person_location = person.get("location", "").strip()

            if not name:
                continue

            first_name = name.split()[0] if name else "there"
            contact_id = hashlib.md5(f"{name}|{profile_url}".encode()).hexdigest()[:16]

            all_contacts.append(Contact(
                contact_id=contact_id,
                name=name,
                first_name=first_name,
                profile_url=profile_url,
                company=company,
                title=title_text,
                location=person_location,
            ))

        # ── Geographic filtering ────────────────────────────────────
        # Prefer Canadian contacts; fall back to North American if needed
        canadian = [c for c in all_contacts if c.location and _is_canadian_location(c.location)]
        north_american = [c for c in all_contacts if c.location and _is_north_american_location(c.location)]
        no_location = [c for c in all_contacts if not c.location]

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
            logger.info(f"  → {len(all_contacts)} people found but none with relevant titles in region, skipping {company}")
            return contacts

        # Sort by relevance — main loop caps at MAX_MESSAGES_PER_COMPANY
        relevant.sort(key=_score_contact, reverse=True)
        contacts = relevant

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
    max_results: int = 20,
    job: Job | None = None,
) -> list[Contact]:
    """
    Search LinkedIn People for employees currently at `company`.
    Returns up to `max_results` contacts.
    Includes 1st-degree connections so we can DM already-connected people.
    """
    # Add "Canada" to keywords so results favour local employees
    geo_keyword = ""
    job_location = (job.location if job else "").strip()
    if _is_canadian_location(job_location) or "canada" in Config.JOB_LOCATION.lower():
        geo_keyword = "%20Canada"

    search_url = (
        f"https://www.linkedin.com/search/results/people/"
        f"?keywords={company}{geo_keyword}"
        f"&network=%5B%22F%22%2C%22S%22%2C%22O%22%5D"
        f"&origin=FACETED_SEARCH"
    )
    # network filter: F = 1st degree, S = 2nd degree, O = 3rd+

    try:
        driver.get(search_url)
        human_delay(1, 1.5)
        scroll_page(driver, scrolls=4)
    except WebDriverException as e:
        logger.debug(f"Browser error navigating to people search: {e}")
        return []

    contacts: list[Contact] = []

    try:
        # Use JS to extract all people data at once — avoids stale element issues
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

        if not people_data:
            logger.debug(f"  Search fallback: no results found for {company}")
            return contacts

        logger.debug(f"  Search fallback: {len(people_data)} people found for {company}")

        for person in people_data[:max_results]:
            name = person.get("name", "").strip()
            profile_url = person.get("link", "").strip()
            title_text = person.get("title", "").strip()
            location = person.get("location", "").strip()

            if not name or not profile_url:
                continue

            first_name = name.split()[0] if name else "there"
            contact_id = hashlib.md5(f"{name}|{profile_url}".encode()).hexdigest()[:16]

            contacts.append(Contact(
                contact_id=contact_id,
                name=name,
                first_name=first_name,
                profile_url=profile_url,
                company=company,
                title=title_text,
                location=location,
            ))

    except WebDriverException as e:
        logger.debug(f"Browser error during people search for {company}: {e}")
    except Exception as e:
        logger.debug(f"Error parsing people search results: {e}")

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
        driver.get(contact.profile_url)
        human_delay(1, 2)

        # ── Anti-detection: profile dwell time ─────────────────────
        # Simulate reading the profile before taking action.
        # A real human scrolls around, moves their mouse, pauses.
        _simulate_profile_reading(driver)
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

        # Focus and type the message character by character
        msg_input.click()
        human_delay(0.3, 0.5)

        for char in message[:300]:
            if ord(char) > 0xFFFF:
                driver.execute_script(
                    "arguments[0].textContent += arguments[1];"
                    "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));",
                    msg_input, char,
                )
                time.sleep(random.uniform(0.05, 0.12))
            else:
                msg_input.send_keys(char)
                time.sleep(random.uniform(0.02, 0.08))

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
    
    ChromeDriver's send_keys() crashes on non-BMP characters (emojis like 🙏🥀👋).
    Strategy: type normal chars one-by-one for human feel, inject emojis via JS.
    """
    import random, time

    buffer = ""
    for char in text:
        # Check if character is outside the Basic Multilingual Plane (BMP)
        if ord(char) > 0xFFFF:
            # First, flush any buffered normal chars
            if buffer:
                for c in buffer:
                    element.send_keys(c)
                    time.sleep(random.uniform(0.02, 0.08))
                buffer = ""
            # Inject the non-BMP char via JavaScript
            element.parent.execute_script(
                "arguments[0].value += arguments[1]; "
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                element, char
            )
            time.sleep(random.uniform(0.05, 0.12))
        else:
            element.send_keys(char)
            time.sleep(random.uniform(0.02, 0.08))


def _simulate_profile_reading(driver: webdriver.Chrome):
    """Simulate a human reading a LinkedIn profile before taking action.

    Real humans don't land on a page and immediately click Connect.
    They scroll around, move their mouse, maybe read the About section.
    This adds 3–7 seconds of natural-looking activity.
    """
    import time

    # Move mouse to a random spot (like reading the headline)
    simulate_random_mouse_movement(driver)

    # Scroll down a bit to "read" the profile (random distance)
    scroll_dist = random.randint(200, 500)
    driver.execute_script(f"window.scrollBy(0, {scroll_dist});")
    human_delay(1.0, 2.5)

    # Move mouse again (like reading a section)
    simulate_random_mouse_movement(driver)

    # Maybe scroll down a bit more (60% chance)
    if random.random() < 0.60:
        driver.execute_script(f"window.scrollBy(0, {random.randint(100, 350)});")
        human_delay(0.5, 1.5)

    # Scroll back up to the top (where Connect button lives)
    driver.execute_script("window.scrollTo(0, 0);")
    human_delay(0.3, 0.7)

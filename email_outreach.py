"""
Email Outreach — discovers corporate emails and sends follow-up emails
after LinkedIn connection requests.

Pipeline:
  1. After the bot sends a LinkedIn connection request, discover_email()
     guesses the contact's corporate email from their name + company.
  2. Next day's run calls send_pending_emails() which sends a short,
     personalized email to every contact whose LinkedIn touch was ≥1 day
     ago and who hasn't been emailed yet.

No paid APIs required — uses company domain guessing + Gmail SMTP.
"""

import random
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from datetime import datetime, timedelta

from config import Config
from models import Database, Contact, Job
from utils import get_logger, human_delay

logger = get_logger("email_outreach")


# ═══════════════════════════════════════════════════════════════════════
#  COMPANY DOMAIN MAPPING
# ═══════════════════════════════════════════════════════════════════════
# Known domains for major Canadian tech/bank employers.
# Format: lowercase company name fragment → (domain, email_format)
# email_format: 'first.last', 'firstlast', 'flast', 'first_last', 'first'

_KNOWN_DOMAINS: dict[str, tuple[str, str]] = {
    # ── FAANG / Big Tech Canada ──────────────────────────────────
    "google": ("google.com", "first.last"),
    "amazon": ("amazon.com", "first.last"),
    "microsoft": ("microsoft.com", "first.last"),
    "apple": ("apple.com", "first.last"),
    "meta": ("meta.com", "first.last"),
    "stripe": ("stripe.com", "first.last"),
    "netflix": ("netflix.com", "first.last"),
    "uber": ("uber.com", "first.last"),
    "salesforce": ("salesforce.com", "first.last"),
    "oracle": ("oracle.com", "first.last"),
    "ibm": ("ibm.com", "first.last"),
    "intel": ("intel.com", "first.last"),
    "nvidia": ("nvidia.com", "first.last"),
    "cisco": ("cisco.com", "first.last"),
    "dell": ("dell.com", "first_last"),
    "sap": ("sap.com", "first.last"),

    # ── Canadian Tech Champions ──────────────────────────────────
    "shopify": ("shopify.com", "first.last"),
    "lightspeed": ("lightspeedhq.com", "first.last"),
    "coveo": ("coveo.com", "first.last"),
    "wealthsimple": ("wealthsimple.com", "first.last"),
    "hootsuite": ("hootsuite.com", "first.last"),
    "bench": ("bench.co", "first.last"),
    "ada": ("ada.cx", "first.last"),
    "clearco": ("clear.co", "first.last"),
    "clio": ("clio.com", "first.last"),
    "ecobee": ("ecobee.com", "first.last"),
    "freshbooks": ("freshbooks.com", "first.last"),
    "kinaxis": ("kinaxis.com", "first.last"),
    "magnet forensics": ("magnetforensics.com", "first.last"),
    "opentext": ("opentext.com", "first.last"),
    "d2l": ("d2l.com", "first.last"),
    "verafin": ("verafin.com", "first.last"),
    "blackberry": ("blackberry.com", "first.last"),
    "properly": ("properly.ca", "first.last"),
    "coconut software": ("coconutsoftware.com", "first.last"),
    "vidyard": ("vidyard.com", "first.last"),
    "tulip": ("tulip.co", "first.last"),
    "fellow": ("fellow.app", "first.last"),
    "clearbanc": ("clearbanc.com", "first.last"),
    "unbounce": ("unbounce.com", "first.last"),
    "procurify": ("procurify.com", "first.last"),
    "thinkific": ("thinkific.com", "first.last"),
    "benevity": ("benevity.com", "first.last"),
    "dialogue": ("dialogue.co", "first.last"),
    "nuvei": ("nuvei.com", "first.last"),
    "dapper labs": ("dapperlabs.com", "first.last"),

    # ── Canadian Banks ───────────────────────────────────────────
    "rbc": ("rbc.com", "first.last"),
    "royal bank": ("rbc.com", "first.last"),
    "td": ("td.com", "first.last"),
    "td bank": ("td.com", "first.last"),
    "scotiabank": ("scotiabank.com", "first.last"),
    "bmo": ("bmo.com", "first.last"),
    "cibc": ("cibc.com", "first.last"),
    "national bank": ("nbc.ca", "first.last"),
    "desjardins": ("desjardins.com", "first.last"),

    # ── Telecom / Enterprise ─────────────────────────────────────
    "telus": ("telus.com", "first.last"),
    "bell": ("bell.ca", "first.last"),
    "rogers": ("rci.rogers.com", "first.last"),
    "shaw": ("sjrb.ca", "first.last"),
    "loblaw": ("loblaw.ca", "first.last"),
    "intact": ("intact.net", "first.last"),
    "sun life": ("sunlife.com", "first.last"),
    "manulife": ("manulife.com", "first.last"),

    # ── Consulting / Staffing ────────────────────────────────────
    "deloitte": ("deloitte.ca", "first.last"),
    "kpmg": ("kpmg.ca", "first.last"),
    "accenture": ("accenture.com", "first.last"),
    "ey": ("ca.ey.com", "first.last"),
    "ernst & young": ("ca.ey.com", "first.last"),
    "pwc": ("pwc.com", "first.last"),
    "cgi": ("cgi.com", "first.last"),
    "capgemini": ("capgemini.com", "first.last"),

    # ── Gov Vendors / IT Services ────────────────────────────────
    "shared services canada": ("canada.ca", "first.last"),
    "nav canada": ("navcanada.ca", "first.last"),
}


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL ADDRESS DISCOVERY
# ═══════════════════════════════════════════════════════════════════════

def _parse_name(full_name: str) -> tuple[str, str]:
    """Extract first and last name from a full name string.

    Handles:
      - "John Smith" → ("john", "smith")
      - "John Paul Smith" → ("john", "smith")  (last word = last name)
      - "Dr. John Smith" → ("john", "smith")   (common prefixes stripped)
      - "John Smith, MBA" → ("john", "smith")  (suffixes stripped)
    """
    # Strip common suffixes after comma
    name = re.sub(r",.*$", "", full_name.strip())
    # Strip common prefixes
    name = re.sub(r"^(dr\.?|mr\.?|ms\.?|mrs\.?|prof\.?)\s+", "", name, flags=re.IGNORECASE)
    # Remove anything in parentheses
    name = re.sub(r"\([^)]*\)", "", name).strip()
    # Remove special chars except hyphens and spaces
    name = re.sub(r"[^\w\s-]", "", name).strip()

    parts = name.lower().split()
    if len(parts) < 2:
        return (parts[0] if parts else "", "")

    return (parts[0], parts[-1])


def _lookup_domain(company: str) -> tuple[str, str] | None:
    """Look up company domain and email format from known mapping.

    Returns (domain, format) or None if not found.
    Uses word-boundary matching to avoid false positives
    (e.g. 'ada' inside 'Canada').
    """
    company_lower = company.lower().strip()

    # Try longest fragments first to prefer specific matches
    # (e.g. "td bank" before "td")
    sorted_items = sorted(
        _KNOWN_DOMAINS.items(), key=lambda x: len(x[0]), reverse=True
    )
    for fragment, (domain, fmt) in sorted_items:
        # Word-boundary match: fragment must appear as a whole word
        # or at the start/end of the company name, not as a substring
        # of a longer word (e.g. "ada" should not match "Canada").
        pattern = r'(?:^|\b|\s)' + re.escape(fragment) + r'(?:$|\b|\s)'
        if re.search(pattern, company_lower):
            return (domain, fmt)

    return None


def _guess_domain(company: str) -> str | None:
    """Guess company domain from company name when not in known list.

    "Acme Corp" → "acmecorp.com"
    "Super Software Inc." → "supersoftware.com"
    """
    # Strip common suffixes
    name = company.lower().strip()
    for suffix in [
        " inc.", " inc", " ltd.", " ltd", " llc", " corp.", " corp",
        " co.", " co", " limited", " technologies", " technology",
        " solutions", " software", " group", " canada", " digital",
        " consulting", " services", " labs", " studio", " studios",
    ]:
        name = name.replace(suffix, "")
    # Remove non-alphanumeric
    name = re.sub(r"[^a-z0-9]", "", name.strip())
    if len(name) < 2:
        return None
    return f"{name}.com"


# ═══════════════════════════════════════════════════════════════════════
#  COMPANY WEBSITE EMAIL PATTERN SCRAPING
# ═══════════════════════════════════════════════════════════════════════
# When a company isn't in our known domain list, try to scrape their
# website for email addresses. Extract the format (first.last, flast,
# etc.) from any emails we find and apply it to the contact's name.

# In-memory cache: domain → format string (persists for the run)
_scraped_format_cache: dict[str, str | None] = {}


def _detect_format_from_email(email_addr: str, domain: str) -> str | None:
    """Given a discovered email like 'john.doe@acme.com', infer the format.

    Returns format string or None if can't determine.
    """
    local = email_addr.split("@")[0].lower()
    # Skip generic addresses
    if local in (
        "info", "hello", "contact", "support", "admin", "sales",
        "hr", "careers", "jobs", "team", "press", "media", "help",
        "office", "general", "marketing", "inquiries", "inquiry",
        "noreply", "no-reply", "webmaster", "abuse", "privacy",
    ):
        return None

    # Check patterns — look for common structures
    if "." in local:
        parts = local.split(".")
        if len(parts) == 2 and len(parts[0]) > 1 and len(parts[1]) > 1:
            return "first.last"
    if "_" in local:
        parts = local.split("_")
        if len(parts) == 2 and len(parts[0]) > 1 and len(parts[1]) > 1:
            return "first_last"
    # flast: 1 char + longer part  (e.g., "jdoe")
    if len(local) >= 4 and local[0].isalpha() and local[1:].isalpha():
        # Could be flast or firstlast — heuristic: if first part is 1 char
        # and rest looks like a last name (4+ chars), it's flast
        if len(local) <= 12:
            # Can't distinguish firstlast from flast without name context
            # Default to the more common format
            return "first.last"

    return None


def _scrape_website_email_pattern(domain: str) -> str | None:
    """Scrape a company website to find email patterns.

    Fetches the main page + /contact and extracts any email addresses
    found. From those, infers the email format the company uses.

    Returns format string ('first.last', 'flast', etc.) or None.
    """
    if domain in _scraped_format_cache:
        return _scraped_format_cache[domain]

    import urllib.request
    import urllib.error
    import signal
    import threading

    # Pages most likely to contain staff email addresses
    paths = ["", "/contact"]
    found_emails: list[str] = []

    def _fetch(url: str, timeout: float = 4) -> str | None:
        """Fetch a URL with a hard wall-clock timeout via threading."""
        result: list[str | None] = [None]

        def _do_fetch():
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/145.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml",
                    },
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    result[0] = resp.read(200_000).decode("utf-8", errors="ignore")
            except Exception:
                pass

        t = threading.Thread(target=_do_fetch, daemon=True)
        t.start()
        t.join(timeout=timeout)
        return result[0]

    for path in paths:
        url = f"https://{domain}{path}"
        html = _fetch(url)
        if html is None:
            continue

        # Extract emails from mailto: links and plain text
        # Pattern: looks for word@domain where domain matches our target
        domain_escaped = re.escape(domain)
        pattern = rf"[a-zA-Z0-9_.+-]+@{domain_escaped}"
        emails = re.findall(pattern, html, re.IGNORECASE)
        found_emails.extend(emails)

        # Also check for mailto: links that might use a subdomain
        mailto_pattern = r'mailto:([a-zA-Z0-9_.+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
        mailto_emails = re.findall(mailto_pattern, html, re.IGNORECASE)
        for me in mailto_emails:
            if domain in me.lower():
                found_emails.append(me)

        # Stop early if we found enough
        if len(found_emails) >= 3:
            break

    if not found_emails:
        _scraped_format_cache[domain] = None
        logger.debug(f"  No emails found on {domain}")
        return None

    # Deduplicate
    found_emails = list(set(e.lower() for e in found_emails))

    # Try to infer format from each found email
    for email_addr in found_emails:
        fmt = _detect_format_from_email(email_addr, domain)
        if fmt:
            _scraped_format_cache[domain] = fmt
            logger.debug(
                f"  Scraped email pattern from {domain}: {fmt} "
                f"(from {email_addr})"
            )
            return fmt

    _scraped_format_cache[domain] = None
    return None


def _format_email(first: str, last: str, domain: str, fmt: str) -> str:
    """Build an email address from name parts, domain and format string."""
    if fmt == "first.last":
        return f"{first}.{last}@{domain}"
    elif fmt == "firstlast":
        return f"{first}{last}@{domain}"
    elif fmt == "flast":
        return f"{first[0]}{last}@{domain}"
    elif fmt == "first_last":
        return f"{first}_{last}@{domain}"
    elif fmt == "first":
        return f"{first}@{domain}"
    # Default
    return f"{first}.{last}@{domain}"


def discover_email(contact: Contact) -> str | None:
    """Guess the corporate email for a LinkedIn contact.

    Returns the most likely email address, or None if we can't
    determine it (e.g. missing name/company info).
    """
    if not contact.company or not contact.name:
        return None

    first, last = _parse_name(contact.name)
    if not first or not last:
        logger.debug(f"  Cannot parse name '{contact.name}' into first/last")
        return None

    # Clean name parts: remove hyphens for email
    first_clean = re.sub(r"[^a-z]", "", first)
    last_clean = re.sub(r"[^a-z]", "", last)
    if not first_clean or not last_clean:
        return None

    # Try known domain first
    known = _lookup_domain(contact.company)
    if known:
        domain, fmt = known
        email = _format_email(first_clean, last_clean, domain, fmt)
        logger.debug(f"  Email (known domain): {email}")
        return email

    # Fall back to domain guessing + website scraping
    domain = _guess_domain(contact.company)
    if not domain:
        logger.debug(f"  Cannot guess domain for '{contact.company}'")
        return None

    # Try scraping the company website for email patterns
    scraped_fmt = _scrape_website_email_pattern(domain)
    fmt = scraped_fmt or "first.last"

    email = _format_email(first_clean, last_clean, domain, fmt)
    logger.debug(f"  Email ({'scraped' if scraped_fmt else 'guessed'} format): {email}")
    return email


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL TEMPLATE SELECTION
# ═══════════════════════════════════════════════════════════════════════

_email_counter = 0


def _pick_email_template(contact: Contact, job: Job) -> tuple[str, str]:
    """Select and format an email template. Returns (subject, body)."""
    global _email_counter

    templates = Config.EMAIL_TEMPLATES
    if not templates:
        return "", ""

    # Recruiter/HR contacts get the recruiter template (last one)
    _recruiter_kw = {"recruiter", "talent", "hiring", "hr ", "human resource",
                     "people operations", "talent acquisition"}
    title_lower = (contact.title or "").lower()
    is_recruiter = any(kw in title_lower for kw in _recruiter_kw)

    if is_recruiter and len(templates) >= 3:
        raw = templates[2]  # E2 — recruiter template
    else:
        # Rotate through E0 and E1
        pool = templates[:2] if len(templates) >= 2 else templates
        raw = pool[_email_counter % len(pool)]
        _email_counter += 1

    # Extract tech snippet (reuse messenger logic via import)
    try:
        from messenger import _extract_tech_from_jd
        tech_snippet = _extract_tech_from_jd(job)
    except (ImportError, Exception):
        tech_snippet = "Java, Python, and cloud tools"

    # Format placeholders
    formatted = raw.format(
        first_name=contact.first_name or contact.name.split()[0],
        job_title=job.title[:50],
        company=contact.company,
        tech_snippet=tech_snippet,
        your_name=Config.YOUR_NAME,
        school=Config.YOUR_SCHOOL,
    )

    # Split subject from body (template format: "Subject: ...\n\n body...")
    lines = formatted.strip().split("\n", 1)
    subject = ""
    body = formatted
    if lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = lines[1].strip() if len(lines) > 1 else ""

    return subject, body


# ═══════════════════════════════════════════════════════════════════════
#  EMAIL SENDING — Gmail SMTP
# ═══════════════════════════════════════════════════════════════════════

def _send_email(to_addr: str, subject: str, body: str) -> bool:
    """Send a plain-text email via Gmail SMTP.

    Uses TLS on port 587 with an App Password.
    Returns True if sent, False on failure.
    """
    email_addr = Config.EMAIL_ADDRESS
    app_password = Config.EMAIL_APP_PASSWORD

    if not email_addr or not app_password:
        logger.error("EMAIL_ADDRESS or EMAIL_APP_PASSWORD not configured")
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{Config.YOUR_NAME} <{email_addr}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Reply-To"] = email_addr

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email_addr, app_password)
            server.sendmail(email_addr, to_addr, msg.as_string())
        logger.info(f"  📧 Email sent to {to_addr}")
        return True
    except smtplib.SMTPRecipientsRefused:
        logger.warning(f"  Recipient rejected: {to_addr} (bad address)")
        return False
    except smtplib.SMTPAuthenticationError:
        logger.error("Gmail auth failed — check EMAIL_APP_PASSWORD. "
                      "Disabling email for this run.")
        # Signal caller to stop trying
        raise
    except (smtplib.SMTPException, OSError) as e:
        logger.warning(f"  Email send failed for {to_addr}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
#  MAIN PIPELINE — called from main.py each run
# ═══════════════════════════════════════════════════════════════════════

def send_pending_emails(db: Database) -> int:
    """Send follow-up emails to contacts who were LinkedIn-touched ≥1 day ago.

    Runs BEFORE the LinkedIn outreach pass (no browser needed).
    Returns the number of emails sent.
    """
    if not Config.EMAIL_ENABLED:
        return 0

    if not Config.EMAIL_ADDRESS or not Config.EMAIL_APP_PASSWORD:
        logger.warning("Email outreach enabled but credentials not set — skipping")
        return 0

    # Check weekly limit
    weekly_sent = db.weekly_emails_sent()
    if weekly_sent >= Config.EMAIL_WEEKLY_LIMIT:
        logger.info(
            f"📧 Weekly email limit reached ({weekly_sent}/{Config.EMAIL_WEEKLY_LIMIT})"
        )
        return 0

    # Check daily limit
    daily_sent = db.daily_emails_sent()
    remaining_daily = Config.EMAIL_DAILY_LIMIT - daily_sent
    remaining_weekly = Config.EMAIL_WEEKLY_LIMIT - weekly_sent
    remaining = min(remaining_daily, remaining_weekly)
    if remaining <= 0:
        logger.info(f"📧 Daily email limit reached ({daily_sent}/{Config.EMAIL_DAILY_LIMIT})")
        return 0

    # Get contacts pending email follow-up
    pending = db.get_pending_email_contacts(
        min_delay_days=Config.EMAIL_DELAY_AFTER_LINKEDIN
    )
    if not pending:
        logger.info("📧 No contacts pending email follow-up")
        return 0

    logger.info(
        f"📧 Email follow-up: {len(pending)} contacts pending, "
        f"will send up to {remaining} (daily: {daily_sent}/{Config.EMAIL_DAILY_LIMIT}, "
        f"weekly: {weekly_sent}/{Config.EMAIL_WEEKLY_LIMIT})"
    )

    sent = 0
    for contact, job in pending:
        if sent >= remaining:
            break

        subject, body = _pick_email_template(contact, job)
        if not subject or not body:
            logger.debug(f"  Skipping {contact.name} — empty email template")
            continue

        try:
            if _send_email(contact.email, subject, body):
                db.mark_email_sent(contact.contact_id)
                db.log_activity("email_sent", f"{contact.name} <{contact.email}>")
                sent += 1

                # Human-like delay between sends (30-60 seconds)
                if sent < remaining and sent < len(pending):
                    delay = random.uniform(30, 60)
                    logger.debug(f"  Waiting {delay:.0f}s before next email")
                    human_delay(30, 60)
        except smtplib.SMTPAuthenticationError:
            # Auth failed — stop all email sending this run
            logger.error("Stopping email outreach — authentication error")
            break

    logger.info(f"📧 Follow-up emails sent: {sent}")
    return sent

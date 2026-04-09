"""
Configuration module — loads settings from .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)


class Config:
    """All configuration values, loaded from environment variables."""

    # ── LinkedIn Credentials ──────────────────────────────────────────
    LINKEDIN_EMAIL: str = os.getenv("LINKEDIN_EMAIL", "")
    LINKEDIN_PASSWORD: str = os.getenv("LINKEDIN_PASSWORD", "")

    # ── Job Search ────────────────────────────────────────────────────
    JOB_KEYWORDS: list[str] = [
        kw.strip()
        for kw in os.getenv("JOB_KEYWORDS", "Software Engineer").split(",")
    ]
    JOB_LOCATION: str = os.getenv("JOB_LOCATION", "Canada")
    EXPERIENCE_LEVEL: list[str] = [
        lvl.strip()
        for lvl in os.getenv("EXPERIENCE_LEVEL", "entry_level,associate").split(",")
    ]
    REMOTE_FILTER: list[str] = [
        r.strip() for r in os.getenv("REMOTE_FILTER", "remote").split(",")
    ]

    # ── Multi-Location Search ─────────────────────────────────────
    # Each tuple: (location_string, [work_type_filters])
    # Single Canada-wide search catches everything in one pass.
    # Toronto/GTA jobs get a scoring boost in _score_job_relevance.
    JOB_SEARCH_LOCATIONS: list[tuple[str, list[str]]] = [
        ("Canada", ["remote", "on_site", "hybrid"]),
    ]

    # How far back to search for job postings (default: 1 week)
    # Wider window = way more jobs per scrape = bigger pool to rank.
    JOB_POSTED_WITHIN: str = os.getenv("JOB_POSTED_WITHIN", "r604800")

    # ── Referral Outreach ─────────────────────────────────────────────
    # CONSERVATIVE limits — the old version ran 25–40/day with 180/week
    # and never got a single warning.  We got greedy → LinkedIn noticed.
    # Rule: it's ALWAYS better to send fewer, higher-quality referrals
    # than to blast 200/day and get restricted.
    DAILY_TARGET_MIN: int = int(os.getenv("DAILY_TARGET_MIN", "25"))
    DAILY_TARGET_MAX: int = int(os.getenv("DAILY_TARGET_MAX", "40"))
    MAX_MESSAGES_PER_DAY: int = DAILY_TARGET_MAX   # overridden at runtime
    # 3 per company keeps it realistic — nobody cold-messages 5 strangers
    # at the same company in one sitting.
    MAX_MESSAGES_PER_COMPANY: int = int(os.getenv("MAX_MESSAGES_PER_COMPANY", "3"))
    # Delay between sends — 30s to 75s.  The bot has 7 layers of
    # protection (fatigue, velocity throttle, breaks, browse-without-acting,
    # feed engagement, page variety, skip days) so the per-send delay
    # just needs to look natural, not be the primary safety lever.
    MESSAGE_DELAY_MIN: int = int(os.getenv("MESSAGE_DELAY_MIN", "20"))
    MESSAGE_DELAY_MAX: int = int(os.getenv("MESSAGE_DELAY_MAX", "50"))

    # ── Weekly Safety Limits (LinkedIn monitors these) ────────────────
    # LinkedIn's actual weekly connection limit is ~100-200 depending on
    # account age, network size, and Premium status.  We stay well under.
    # After the warning: cut everything roughly in HALF from the old limits.
    MAX_PROFILE_VIEWS_PER_WEEK: int = int(os.getenv("MAX_PROFILE_VIEWS_PER_WEEK", "800"))
    MAX_CONNECTIONS_PER_WEEK: int = int(os.getenv("MAX_CONNECTIONS_PER_WEEK", "200"))

    # ── Contact Filtering ─────────────────────────────────────────────
    CONTACT_BLOCK_WORDS: list[str] = [
        kw.strip().lower()
        for kw in os.getenv(
            "CONTACT_BLOCK_WORDS",
            "student,intern,freelancer,freelance,self-employed,"
            "university,college,school,institute,professor,teacher,"
            "youtuber,volunteer,unemployed",
        ).split(",")
    ]

    # ── Post Hunting ("Hiring" posts on LinkedIn feed) ────────────────
    POST_HUNT_ENABLED: bool = os.getenv("POST_HUNT_ENABLED", "true").lower() == "true"

    # --- Build POST_HUNT_KEYWORDS dynamically from JOB_KEYWORDS -------
    # Each role gets paired with every hiring phrase, then we add
    # generic "we're hiring" / "open to work" variations on top.
    _HIRING_PHRASES: list[str] = [
        "hiring {role}",
        "we're hiring {role}",
        "we are hiring {role}",
        "my team is hiring {role}",
        "our team is hiring {role}",
        "looking for {role}",
        "looking for a {role}",
        "open role {role}",
        "open position {role}",
        "join my team {role}",
        "join our team {role}",
        "{role} needed",
        "{role} wanted",
        "{role} opening",
        "{role} position",
        "hiring a {role}",
        "come work with us {role}",
        "new role {role}",
        "{role} role",
        "{role} opportunity",
    ]
    # Generic hiring phrases (no role, just catch-all)
    _GENERIC_HIRING_PHRASES: list[str] = [
        "we're hiring Canada",
        "hiring in Canada",
        "hiring in Toronto",
        "hiring in Vancouver",
        "hiring in Montreal",
        "hiring in Ottawa",
        "hiring in Calgary",
        "hiring engineers Canada",
        "my team is hiring",
        "open roles engineering",
        "come join our engineering team",
        "engineering team is growing",
        "growing our team engineers",
        "multiple openings engineer",
        "urgently hiring developer",
        "immediately hiring engineer",
        "tech hiring Canada",
        "startup hiring engineers",
        # Canada-specific patterns that yield better results
        "hiring Toronto software",
        "hiring Vancouver developer",
        "hiring Montreal engineer",
        "hiring Ottawa tech",
        "team growing Toronto",
        "team growing Vancouver",
        "join us Toronto engineer",
        "join us Vancouver developer",
        "open role Toronto",
        "open role Vancouver",
        "open role Montreal",
        "Canada remote developer",
        "Canada remote engineer",
        "Canadian tech company hiring",
        "Canadian startup hiring",
        "GTA hiring developer",
        "Ontario hiring engineer",
        "BC hiring developer",
        "Alberta hiring engineer",
        "waterloo hiring software",
        "kitchener hiring engineer",
        "hiring hybrid Toronto",
        "hiring hybrid Vancouver",
        "referral Canada software",
        "referral Toronto developer",
    ]

    # Build the full keyword list: role × phrase + generics
    _role_keywords_raw: list[str] = []
    for _role in JOB_KEYWORDS:
        # Normalise: "Full-Stack Developer" → "full stack developer"
        _role_norm = _role.lower().replace("-", " ")
        # Also make a short version: "Backend Engineer" → "backend"
        _role_short = _role_norm.split()[0] if _role_norm else _role_norm
        for _phrase in _HIRING_PHRASES:
            _role_keywords_raw.append(_phrase.format(role=_role_norm))
            # Short variant only if the short form is meaningful (≥6 chars)
            # Avoids useless queries like "hiring full" or "looking for cloud"
            if _role_short != _role_norm and len(_role_short) >= 6:
                _role_keywords_raw.append(_phrase.format(role=_role_short))
    _role_keywords_raw.extend(_GENERIC_HIRING_PHRASES)
    # Deduplicate while preserving order
    _seen_kw: set[str] = set()
    POST_HUNT_KEYWORDS: list[str] = []
    for _kw in _role_keywords_raw:
        _lower = _kw.strip().lower()
        if _lower not in _seen_kw:
            _seen_kw.add(_lower)
            POST_HUNT_KEYWORDS.append(_kw.strip())
    del _role_keywords_raw, _seen_kw, _kw, _lower  # clean up temps
    # Max posts to engage with per run (keep it very human)
    POST_HUNT_MAX_PER_RUN: int = int(os.getenv("POST_HUNT_MAX_PER_RUN", "8"))
    # Max post engagements per week (separate from connection budget)
    POST_HUNT_MAX_PER_WEEK: int = int(os.getenv("POST_HUNT_MAX_PER_WEEK", "25"))
    # Minimum legitimacy score (0-100) to engage with a post
    POST_HUNT_MIN_SCORE: int = int(os.getenv("POST_HUNT_MIN_SCORE", "25"))

    # ── Schedule ──────────────────────────────────────────────────────
    DAILY_RUN_HOUR: int = int(os.getenv("DAILY_RUN_HOUR", "9"))
    DAILY_RUN_MINUTE: int = int(os.getenv("DAILY_RUN_MINUTE", "0"))

    # ── Browser ───────────────────────────────────────────────────────
    HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"
    CHROME_PROFILE_PATH: str = os.getenv("CHROME_PROFILE_PATH", "")

    # ── Database ──────────────────────────────────────────────────────
    DB_PATH: str = os.getenv("DB_PATH", str(Path(__file__).parent / "data" / "jobs.db"))

    # ── Logging ───────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ── Email Outreach ────────────────────────────────────────────────
    # After a LinkedIn connection request, the bot discovers the contact's
    # corporate email and sends a short follow-up email the next day.
    # Uses Gmail SMTP with an App Password (enable 2FA on Google, then
    # generate an App Password at myaccount.google.com/apppasswords).
    EMAIL_ENABLED: bool = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
    EMAIL_ADDRESS: str = os.getenv("EMAIL_ADDRESS", "")
    EMAIL_APP_PASSWORD: str = os.getenv("EMAIL_APP_PASSWORD", "")
    EMAIL_DAILY_LIMIT: int = int(os.getenv("EMAIL_DAILY_LIMIT", "10"))
    EMAIL_WEEKLY_LIMIT: int = int(os.getenv("EMAIL_WEEKLY_LIMIT", "50"))
    # Days to wait after LinkedIn touch before sending email
    EMAIL_DELAY_AFTER_LINKEDIN: int = int(os.getenv("EMAIL_DELAY_AFTER_LINKEDIN", "1"))

    # Email templates — plain text, short, professional.
    # Placeholders: {first_name}, {job_title}, {company}, {tech_snippet},
    #               {your_name}, {school}
    # Format: first line = "Subject: ..." then blank line then body.
    EMAIL_TEMPLATES: list[str] = [
        # E0 — Standard follow-up
        "Subject: Quick note \u2014 {job_title} application\n\n"
        "Hi {first_name},\n\n"
        "I recently applied for the {job_title} role at {company} "
        "and wanted to reach out directly.\n\n"
        "Quick background: I'm a software developer with Canadian "
        "co-op experience (Ontario Ministry) and hands-on work with "
        "{tech_snippet}. I've built and shipped production systems "
        "end-to-end.\n\n"
        "Would love to connect or learn more about the team. Happy "
        "to share my portfolio if that'd be helpful.\n\n"
        "Best,\n{your_name}",

        # E1 — Shorter variant
        "Subject: {job_title} at {company} \u2014 applied + wanted to connect\n\n"
        "Hi {first_name},\n\n"
        "Applied for the {job_title} position and wanted to put a "
        "face to the application. I've worked with {tech_snippet} in "
        "production and have Canadian co-op experience.\n\n"
        "Would appreciate any insight into the role or team. No "
        "pressure at all.\n\n"
        "Best,\n{your_name}",

        # E2 — Recruiter/HR specific
        "Subject: {job_title} \u2014 applied via portal, reaching out directly\n\n"
        "Hi {first_name},\n\n"
        "I submitted my application for the {job_title} role at "
        "{company} and wanted to connect directly. I have Canadian "
        "co-op experience (Ontario Ministry) and production experience "
        "with {tech_snippet}.\n\n"
        "Happy to send over my portfolio or jump on a quick call "
        "whenever works for you.\n\n"
        "Best,\n{your_name}",
    ]

    # ── Message Templates ─────────────────────────────────────────────
    # LinkedIn limits connection notes to 300 characters.
    # Strategy: Direct but human. Mention the role, ask clearly, stay short.
    # Your friend proved 5 referrals/week with a direct ask — so we ask.
    # Rotate templates so LinkedIn can't pattern-match identical messages.
    YOUR_NAME: str = os.getenv("YOUR_NAME", "Harsh")
    YOUR_SCHOOL: str = os.getenv("YOUR_SCHOOL", "Seneca College")

    # Each template MUST stay under 300 chars after formatting.
    # Available placeholders: {first_name}, {job_title}, {company}, {your_name}, {school}, {tech_snippet}
    # {tech_snippet} is auto-extracted from the job description (e.g. "Python, React, and AWS")
    REFERRAL_TEMPLATES: list[str] = [
        # ── Original templates (T0–T4): hardcoded tech mentions ──────

        # T0 — Full-stack / frontend roles
        "Hi {first_name}, noticed you're at {company}, I've been"
        " building full-stack apps and automation frameworks with"
        " Java, Python, and React. The {job_title} role really caught"
        " my eye. Market's been tough, a referral would mean a lot"
        " \U0001f64f {your_name}",

        # T1 — Backend / microservices / API roles
        "Hi {first_name}, saw you're at {company} and wanted to reach"
        " out, I've built microservices and CI/CD pipelines with"
        " Java, Python, and AWS. Really interested in the {job_title}"
        " role. Market's tough rn, a referral would mean the world"
        " \U0001f940 {your_name}",

        # T2 — School alum hook (auto-used for school matches)
        "Hi {first_name}, fellow {school} grad here! \U0001f44b I've"
        " been working as a software dev building full-stack apps and"
        " automation tools, the {job_title} role at {company} looks"
        " great. Would really appreciate a referral, it'd mean a lot"
        " \U0001f64f {your_name}",

        # T3 — Cloud / DevOps / infra roles
        "Hi {first_name}, noticed you're at {company}, I've worked"
        " with AWS, Azure DevOps, Docker, and built cloud"
        " microservices + CI/CD pipelines. The {job_title} role is a"
        " great fit. Job market's been rough, a referral would really"
        " help \U0001f64f {your_name}",

        # T4 — Catch-all / generic SWE
        "Hi {first_name}, I see you're at {company} and wanted to"
        " reach out, I'm a software dev with experience in Java,"
        " Python, and cloud infra. Applying for the {job_title} role."
        " Market's really tough rn, a referral would mean a lot to me"
        " \U0001f940 {your_name}",

        # ── JD-aware templates (T5–T7): use {tech_snippet} from desc ──

        # T5 — Short & direct, JD tech
        "Hi {first_name}, noticed you're at {company}, I've been"
        " building production apps and shipping features with"
        " {tech_snippet}. The {job_title} role caught my eye."
        " Market's been rough, a referral would honestly mean the"
        " world \U0001f64f {your_name}",

        # T6 — Value prop, JD tech
        "Hi {first_name}, saw you're at {company} and wanted to"
        " reach out, I've built and deployed real systems using"
        " {tech_snippet}. Really interested in the {job_title}"
        " role. Market's tough rn, a referral would go a long way"
        " \U0001f64f {your_name}",

        # T7 — Recruiter / talent-specific, JD tech
        "Hi {first_name}, saw you handle talent at {company},"
        " I applied for the {job_title} role and wanted to"
        " connect directly. I've shipped production systems"
        " with {tech_snippet}. Would love a chance to chat"
        " about the role \U0001f64f {your_name}",
    ]

    # Legacy single-template fallback (kept for backward compat)
    REFERRAL_MESSAGE_TEMPLATE: str = REFERRAL_TEMPLATES[0]

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of missing or invalid config values."""
        issues = []
        if not cls.LINKEDIN_EMAIL:
            issues.append("LINKEDIN_EMAIL is not set")
        elif "@" not in cls.LINKEDIN_EMAIL:
            issues.append("LINKEDIN_EMAIL doesn't look like a valid email")
        if not cls.LINKEDIN_PASSWORD:
            issues.append("LINKEDIN_PASSWORD is not set")
        # Bounds-check numeric limits to catch typos / bad .env values
        if cls.DAILY_TARGET_MIN < 1 or cls.DAILY_TARGET_MAX > 200:
            issues.append(
                f"DAILY_TARGET range {cls.DAILY_TARGET_MIN}–{cls.DAILY_TARGET_MAX} "
                f"looks wrong (expected 1–200)"
            )
        if cls.MAX_CONNECTIONS_PER_WEEK > 500:
            issues.append(
                f"MAX_CONNECTIONS_PER_WEEK={cls.MAX_CONNECTIONS_PER_WEEK} is dangerously high"
            )
        if cls.MESSAGE_DELAY_MIN < 5:
            issues.append(
                f"MESSAGE_DELAY_MIN={cls.MESSAGE_DELAY_MIN}s is too aggressive (min 5)"
            )
        return issues

    def __repr__(self) -> str:
        """Mask password in any repr / traceback / log output."""
        return (
            f"<Config email={self.LINKEDIN_EMAIL!r} "
            f"password={'*' * 8} headless={self.HEADLESS}>"
        )

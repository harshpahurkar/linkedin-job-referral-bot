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
    # Strategy: cast the widest net possible, minimize overlap.
    # Canada-wide already catches Toronto/Vancouver/etc for on-site.
    # US/remote combos catch companies offering remote to Canadians.
    JOB_SEARCH_LOCATIONS: list[tuple[str, list[str]]] = [
        ("Canada", ["remote", "on_site", "hybrid"]),           # all Canadian jobs
        ("Toronto, Ontario, Canada", ["remote", "hybrid"]),    # TO remote/hybrid (different results than Canada-wide)
        ("Vancouver, British Columbia, Canada", ["remote", "hybrid"]),  # BC remote/hybrid
    ]

    # How far back to search for job postings (default: 1 week)
    # Wider window = way more jobs per scrape = bigger pool to rank.
    JOB_POSTED_WITHIN: str = os.getenv("JOB_POSTED_WITHIN", "r604800")

    # ── Referral Outreach ─────────────────────────────────────────────
    # Budget: 200 connections/week hard cap (the REAL limiter).
    # Daily targets are set high because in practice 50–70% of
    # companies get skipped (no contacts, already messaged, failed sends).
    # The weekly cap stops us before we overdo it.
    DAILY_TARGET_MIN: int = int(os.getenv("DAILY_TARGET_MIN", "80"))
    DAILY_TARGET_MAX: int = int(os.getenv("DAILY_TARGET_MAX", "100"))
    MAX_MESSAGES_PER_DAY: int = DAILY_TARGET_MAX   # overridden at runtime
    # 5 per company × many companies = wide net, weekly cap = safety.
    MAX_MESSAGES_PER_COMPANY: int = int(os.getenv("MAX_MESSAGES_PER_COMPANY", "5"))
    MESSAGE_DELAY_MIN: int = int(os.getenv("MESSAGE_DELAY_MIN", "60"))
    MESSAGE_DELAY_MAX: int = int(os.getenv("MESSAGE_DELAY_MAX", "180"))

    # ── Weekly Safety Limits (LinkedIn monitors these) ────────────────
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
        " I've been building production systems with"
        " {tech_snippet}. The {job_title} role is a great fit."
        " Market's been tough, would love a shot at getting"
        " referred \U0001f64f {your_name}",
    ]

    # Legacy single-template fallback (kept for backward compat)
    REFERRAL_MESSAGE_TEMPLATE: str = REFERRAL_TEMPLATES[0]

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of missing critical config values."""
        issues = []
        if not cls.LINKEDIN_EMAIL:
            issues.append("LINKEDIN_EMAIL is not set")
        if not cls.LINKEDIN_PASSWORD:
            issues.append("LINKEDIN_PASSWORD is not set")
        return issues

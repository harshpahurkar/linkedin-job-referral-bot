"""
Data models for jobs, companies, and contacts — stored in SQLite.
"""

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from config import Config


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    date_scraped: str = field(default_factory=lambda: datetime.now().isoformat())
    applied: bool = False
    referral_requested: bool = False


@dataclass
class Contact:
    contact_id: str
    name: str
    first_name: str
    profile_url: str
    company: str
    title: str = ""
    location: str = ""
    messaged: bool = False
    message_date: str = ""
    connected: bool = False


class Database:
    """Simple SQLite wrapper for persisting jobs and contacts."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or Config.DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
        self._set_db_permissions()
        self._purge_old_activity()

    # ── Schema ────────────────────────────────────────────────────────
    def _init_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id       TEXT PRIMARY KEY,
                title        TEXT,
                company      TEXT,
                location     TEXT,
                url          TEXT,
                description  TEXT,
                date_scraped TEXT,
                applied      INTEGER DEFAULT 0,
                referral_requested INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS contacts (
                contact_id   TEXT PRIMARY KEY,
                name         TEXT,
                first_name   TEXT,
                profile_url  TEXT,
                company      TEXT,
                title        TEXT,
                location     TEXT DEFAULT '',
                messaged     INTEGER DEFAULT 0,
                message_date TEXT,
                connected    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date  TEXT,
                jobs_found     INTEGER,
                messages_sent  INTEGER
            );

            CREATE TABLE IF NOT EXISTS weekly_activity (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type     TEXT,
                action_date     TEXT,
                detail          TEXT
            );

            CREATE TABLE IF NOT EXISTS hiring_posts (
                post_id         TEXT PRIMARY KEY,
                poster_name     TEXT,
                poster_title    TEXT,
                poster_url      TEXT,
                company         TEXT,
                post_text       TEXT,
                post_url        TEXT,
                score           INTEGER DEFAULT 0,
                action_taken    TEXT DEFAULT '',
                date_found      TEXT,
                engaged         INTEGER DEFAULT 0
            );
            """
        )
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Add columns that may be missing in older databases."""
        # Get existing columns for the contacts table
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(contacts)").fetchall()
        }
        if "location" not in cols:
            self.conn.execute(
                "ALTER TABLE contacts ADD COLUMN location TEXT DEFAULT ''"
            )
            self.conn.commit()

    def _set_db_permissions(self):
        """Restrict DB file to owner-only access (Windows: remove inheritance)."""
        try:
            db_file = Path(self.db_path)
            if db_file.exists():
                # Owner read/write only (0o600)
                os.chmod(str(db_file), 0o600)
        except OSError:
            pass  # may fail on some Windows configs — non-critical

    def _purge_old_activity(self, days: int = 90):
        """Delete weekly_activity records older than `days` to limit PII retention."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        self.conn.execute(
            "DELETE FROM weekly_activity WHERE action_date < ?", (cutoff,)
        )
        self.conn.commit()

    # ── Jobs ──────────────────────────────────────────────────────────
    def clear_jobs(self):
        """Delete all jobs so each run starts with a fresh scrape."""
        self.conn.execute("DELETE FROM jobs")
        self.conn.commit()

    def insert_job(self, job: Job) -> bool:
        """Insert a job if it doesn't already exist. Returns True if new."""
        try:
            self.conn.execute(
                """INSERT INTO jobs
                   (job_id, title, company, location, url, description, date_scraped)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.title,
                    job.company,
                    job.location,
                    job.url,
                    job.description,
                    job.date_scraped,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # already exists

    def job_exists(self, job_id: str) -> bool:
        """Check if a job with this ID is already in the database."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return row is not None

    def get_jobs_without_referral(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE referral_requested = 0"
        ).fetchall()
        return [Job(**dict(r)) for r in rows]

    def mark_referral_requested(self, job_id: str):
        self.conn.execute(
            "UPDATE jobs SET referral_requested = 1 WHERE job_id = ?", (job_id,)
        )
        self.conn.commit()

    # ── Contacts ──────────────────────────────────────────────────────
    def insert_contact(self, contact: Contact) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO contacts
                   (contact_id, name, first_name, profile_url, company, title, location)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact.contact_id,
                    contact.name,
                    contact.first_name,
                    contact.profile_url,
                    contact.company,
                    contact.title,
                    contact.location,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_messaged(self, contact_id: str):
        self.conn.execute(
            "UPDATE contacts SET messaged = 1, message_date = ? WHERE contact_id = ?",
            (datetime.now().isoformat(), contact_id),
        )
        self.conn.commit()

    def already_messaged(self, contact_id: str) -> bool:
        row = self.conn.execute(
            "SELECT messaged FROM contacts WHERE contact_id = ?", (contact_id,)
        ).fetchone()
        return bool(row and row["messaged"])

    # ── Run Log ───────────────────────────────────────────────────────
    def log_run(self, jobs_found: int, messages_sent: int):
        self.conn.execute(
            "INSERT INTO run_log (run_date, jobs_found, messages_sent) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), jobs_found, messages_sent),
        )
        self.conn.commit()

    # ── Weekly Activity Tracking ────────────────────────────────────
    def log_activity(self, action_type: str, detail: str = ""):
        """Log a profile_view or connection_request for weekly tracking."""
        self.conn.execute(
            "INSERT INTO weekly_activity (action_type, action_date, detail) VALUES (?, ?, ?)",
            (action_type, datetime.now().isoformat(), detail),
        )
        self.conn.commit()

    def get_weekly_count(self, action_type: str) -> int:
        """Count actions of a given type in the last 7 days."""
        seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM weekly_activity WHERE action_type = ? AND action_date >= ?",
            (action_type, seven_days_ago),
        ).fetchone()
        return row["cnt"] if row else 0

    def weekly_profiles_viewed(self) -> int:
        return self.get_weekly_count("profile_view")

    def weekly_connections_sent(self) -> int:
        return self.get_weekly_count("connection_request")

    # ── Hiring Posts ─────────────────────────────────────────────────
    def insert_hiring_post(self, post_id: str, poster_name: str,
                           poster_title: str, poster_url: str,
                           company: str, post_text: str, post_url: str,
                           score: int) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO hiring_posts
                   (post_id, poster_name, poster_title, poster_url,
                    company, post_text, post_url, score, date_found)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (post_id, poster_name, poster_title, poster_url,
                 company, post_text, post_url, score,
                 datetime.now().isoformat()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def hiring_post_exists(self, post_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM hiring_posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None

    def mark_post_engaged(self, post_id: str, action: str):
        self.conn.execute(
            "UPDATE hiring_posts SET engaged = 1, action_taken = ? WHERE post_id = ?",
            (action, post_id),
        )
        self.conn.commit()

    def mark_post_skipped(self, post_id: str, reason: str):
        """Record skip reason without counting toward engagement limit."""
        self.conn.execute(
            "UPDATE hiring_posts SET action_taken = ? WHERE post_id = ?",
            (reason, post_id),
        )
        self.conn.commit()

    def weekly_post_engagements(self) -> int:
        seven_days_ago = (datetime.now() - timedelta(days=7)).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM hiring_posts "
            "WHERE engaged = 1 AND action_taken IN ('connection_sent', 'dm_sent') "
            "AND date_found >= ?",
            (seven_days_ago,),
        ).fetchone()
        return row["cnt"] if row else 0

    def close(self):
        self.conn.close()

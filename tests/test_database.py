from datetime import datetime, timedelta

from models import Contact, Database, Job


def test_database_dedupes_jobs_and_contacts(tmp_path):
    db = Database(str(tmp_path / "jobs.db"))
    job = Job("job-1", "Software Engineer", "Acme", "Toronto", "https://example.com/job")
    contact = Contact(
        "contact-1",
        "Jane Doe",
        "Jane",
        "https://linkedin.com/in/jane-doe",
        "Acme",
        "Software Engineer",
        "Toronto, Ontario, Canada",
    )

    assert db.insert_job(job)
    assert not db.insert_job(job)
    assert db.job_exists("job-1")

    assert db.insert_contact(contact)
    assert not db.insert_contact(contact)
    assert not db.already_messaged("contact-1")

    db.mark_messaged("contact-1")
    assert db.already_messaged("contact-1")
    db.close()


def test_weekly_activity_counts_only_recent_rows(tmp_path):
    db = Database(str(tmp_path / "jobs.db"))
    old_date = (datetime.now() - timedelta(days=8)).isoformat()

    db.log_activity("profile_view", "recent")
    db.conn.execute(
        "INSERT INTO weekly_activity (action_type, action_date, detail) VALUES (?, ?, ?)",
        ("profile_view", old_date, "old"),
    )
    db.conn.commit()

    assert db.weekly_profiles_viewed() == 1
    db.close()


def test_contacts_schema_has_no_email_outreach_columns(tmp_path):
    db = Database(str(tmp_path / "jobs.db"))
    cols = {
        row[1]
        for row in db.conn.execute("PRAGMA table_info(contacts)").fetchall()
    }

    assert "email" not in cols
    assert "email_sent" not in cols
    assert "email_sent_date" not in cols
    db.close()

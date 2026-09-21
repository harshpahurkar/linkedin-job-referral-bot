from datetime import date, datetime, timedelta

import antidetect
import main
from antidetect import SessionTracker
from messenger import find_and_message_employees
from models import Contact, Database, Job

from test_safety_limits import _BudgetDb, _wire


# ── Warning freeze ───────────────────────────────────────────────────

def test_a_warning_freezes_the_bot_across_runs(freeze_file):
    SessionTracker().flag_warning("HTTP 429")

    antidetect.reset_session()  # what the next run does

    assert antidetect.frozen_reason()
    assert "HTTP 429" in freeze_file.read_text(encoding="utf-8")


def test_pipeline_refuses_to_run_while_frozen(freeze_file, monkeypatch):
    freeze_file.write_text("weekly invitation limit", encoding="utf-8")

    def no_db():
        raise AssertionError("a frozen bot must not get as far as opening the DB")
    monkeypatch.setattr(main, "Database", no_db)

    assert main.run_pipeline() is None


def test_weekly_invite_limit_freezes_the_bot(monkeypatch):
    _wire(monkeypatch, contacts_per_company=3)
    monkeypatch.setattr("messenger.get_session", antidetect.get_session)
    monkeypatch.setattr("messenger._send_connection_with_note", lambda *_a, **_k: "weekly_limit")
    jobs = [Job("j0", "Software Engineer", "Co0", "Toronto, ON", "https://example.com/0")]

    find_and_message_employees(driver=object(), db=_BudgetDb(), jobs=jobs)

    assert antidetect.frozen_reason()


# ── Daily ramp ───────────────────────────────────────────────────────

def test_ramp_adds_five_a_day_up_to_the_ceiling(tmp_path):
    state = tmp_path / "ramp.json"
    day = date(2026, 9, 21)  # a Monday, so no weekend cut
    levels = [main.todays_target(state, day + timedelta(days=i))[0] for i in range(6)]
    assert levels == [30, 35, 40, 45, 50, 50]


def test_target_is_stable_within_a_day(tmp_path):
    state = tmp_path / "ramp.json"
    day = date(2026, 9, 21)
    assert main.todays_target(state, day) == main.todays_target(state, day)


def test_target_never_exceeds_the_level(tmp_path):
    level, target = main.todays_target(tmp_path / "ramp.json", date(2026, 9, 21))
    assert 0 < target <= level


# ── Sends already made today ─────────────────────────────────────────

def test_sends_today_counts_todays_invites_and_dms_only(tmp_path):
    db = Database(str(tmp_path / "jobs.db"))
    db.log_activity("connection_request", "A")
    db.log_activity("direct_message", "B")
    db.log_activity("profile_view", "C")
    db.conn.execute(
        "INSERT INTO weekly_activity (action_type, action_date, detail) VALUES (?, ?, ?)",
        ("connection_request", (datetime.now() - timedelta(days=1)).isoformat(), "yesterday"),
    )
    assert db.sends_today() == 2
    db.close()


def test_company_cap_holds_across_sessions_in_a_day(tmp_path, monkeypatch):
    _wire(monkeypatch, contacts_per_company=3)
    db = Database(str(tmp_path / "jobs.db"))
    for i in range(3):
        c = Contact(f"old{i}", "Old Hand", "Old", f"https://linkedin.com/in/old{i}", "Co0",
                    "Software Engineer", "Toronto, ON")
        db.insert_contact(c)
        db.mark_messaged(c.contact_id)
    jobs = [Job("j0", "Software Engineer", "Co0", "Toronto, ON", "https://example.com/0")]

    # 3 people at Co0 already heard from us today, and the cap is 3.
    assert find_and_message_employees(driver=object(), db=db, jobs=jobs) == 0
    db.close()


# ── 17:00 stop ───────────────────────────────────────────────────────

def test_nothing_is_sent_after_the_stop_time(monkeypatch):
    _wire(monkeypatch, contacts_per_company=3)
    jobs = [Job("j0", "Software Engineer", "Co0", "Toronto, ON", "https://example.com/0")]
    sent = find_and_message_employees(
        driver=object(), db=_BudgetDb(), jobs=jobs,
        stop_at=datetime.now() - timedelta(seconds=1),
    )
    assert sent == 0


# ── Several sessions a day ───────────────────────────────────────────

def test_a_profile_viewed_this_week_is_not_viewed_again(tmp_path, monkeypatch):
    _wire(monkeypatch, contacts_per_company=1)
    db = Database(str(tmp_path / "jobs.db"))
    db.log_activity("profile_view", "https://linkedin.com/in/Co00")
    jobs = [Job("j0", "Software Engineer", "Co0", "Toronto, ON", "https://example.com/0")]

    assert find_and_message_employees(driver=object(), db=db, jobs=jobs) == 0
    assert db.weekly_profiles_viewed() == 1
    db.close()


def test_sessions_only_run_inside_the_workday():
    at = lambda h, m=0: datetime(2026, 9, 21, h, m)
    assert main.session_size(at(7, 59), target=30, sent=0, frozen="") == 0
    assert main.session_size(at(16, 30), target=30, sent=0, frozen="") == 0
    assert 1 <= main.session_size(at(8), target=30, sent=0, frozen="") <= 18


def test_no_session_once_the_target_is_met_or_the_bot_is_frozen():
    noon = datetime(2026, 9, 21, 12)
    assert main.session_size(noon, target=30, sent=30, frozen="") == 0
    assert main.session_size(noon, target=30, sent=0, frozen="HTTP 429") == 0
    assert main.session_size(noon, target=30, sent=28, frozen="") <= 2

from models import Contact, Job
from messenger import find_and_message_employees


class DummyDb:
    def __init__(self, views):
        self.views = views
        self.logged = []

    def weekly_connections_sent(self):
        return 0

    def weekly_profiles_viewed(self):
        return self.views

    def already_messaged(self, _contact_id):
        return False

    def insert_contact(self, _contact):
        return True

    def log_activity(self, action_type, detail=""):
        self.logged.append((action_type, detail))


def test_outreach_stops_before_exceeding_profile_view_limit(monkeypatch):
    monkeypatch.setattr("messenger.Config.MAX_PROFILE_VIEWS_PER_WEEK", 1)
    monkeypatch.setattr("messenger.Config.MAX_CONNECTIONS_PER_WEEK", 100)
    monkeypatch.setattr("messenger.Config.MAX_MESSAGES_PER_DAY", 5)
    monkeypatch.setattr("messenger.Config.MAX_MESSAGES_PER_COMPANY", 3)
    monkeypatch.setattr("messenger.is_session_safe", lambda: True)
    monkeypatch.setattr("messenger.should_take_break", lambda: False)
    monkeypatch.setattr(
        "messenger._browse_company_people_page",
        lambda *_args, **_kwargs: [
            Contact("1", "One Engineer", "One", "https://linkedin.com/in/one", "Acme", "Software Engineer", "Toronto, ON"),
            Contact("2", "Two Engineer", "Two", "https://linkedin.com/in/two", "Acme", "Software Engineer", "Toronto, ON"),
        ],
    )
    monkeypatch.setattr("messenger._send_connection_with_note", lambda *_args, **_kwargs: "failed")

    db = DummyDb(views=0)
    sent = find_and_message_employees(
        driver=object(),
        db=db,
        jobs=[Job("job-1", "Software Engineer", "Acme", "Toronto, ON", "https://example.com/job")],
    )

    assert sent == 0
    assert len([row for row in db.logged if row[0] == "profile_view"]) == 1


class _BudgetDb(DummyDb):
    """DummyDb plus the write methods the success path calls."""
    def __init__(self):
        super().__init__(views=0)
        self.messaged = []

    def mark_messaged(self, contact_id):
        self.messaged.append(contact_id)

    def mark_referral_requested(self, _job_id):
        return True


class _FakeSession:
    def record_connection(self):
        pass

    def record_dm(self):
        pass


def _wire(monkeypatch, contacts_per_company):
    monkeypatch.setattr("messenger.Config.MAX_PROFILE_VIEWS_PER_WEEK", 1000)
    monkeypatch.setattr("messenger.Config.MAX_CONNECTIONS_PER_WEEK", 1000)
    monkeypatch.setattr("messenger.Config.MAX_MESSAGES_PER_DAY", 40)
    monkeypatch.setattr("messenger.Config.MAX_MESSAGES_PER_COMPANY", 3)
    monkeypatch.setattr("messenger.is_session_safe", lambda: True)
    monkeypatch.setattr("messenger.should_take_break", lambda: False)
    monkeypatch.setattr("messenger.get_session", lambda: _FakeSession())
    monkeypatch.setattr("messenger.random.random", lambda: 0.99)  # never skip
    monkeypatch.setattr("messenger.human_delay", lambda *a, **k: None)
    monkeypatch.setattr("messenger.smart_delay", lambda *a, **k: None)
    monkeypatch.setattr("messenger.check_for_linkedin_warnings", lambda _d: (False, ""))

    def people(_driver, _db, company, job=None):
        return [
            Contact(f"{company}-{i}", f"P{i}", "P", f"https://linkedin.com/in/{company}{i}",
                    company, "Software Engineer", "Toronto, ON")
            for i in range(contacts_per_company)
        ]
    monkeypatch.setattr("messenger._browse_company_people_page", people)
    monkeypatch.setattr("messenger._send_connection_with_note",
                        lambda *_a, **_k: "connection_sent")


def test_max_to_send_caps_a_batch(monkeypatch):
    """A window must not send past the remaining daily budget handed to it."""
    _wire(monkeypatch, contacts_per_company=3)
    jobs = [Job(f"j{i}", "Software Engineer", f"Co{i}", "Toronto, ON",
                f"https://example.com/{i}") for i in range(5)]  # 15 contacts available
    sent = find_and_message_employees(driver=object(), db=_BudgetDb(),
                                      jobs=jobs, max_to_send=4)
    assert sent == 4


def test_zero_budget_sends_nothing(monkeypatch):
    """Once the day's target is met, a further window sends nothing."""
    _wire(monkeypatch, contacts_per_company=3)
    jobs = [Job("j0", "Software Engineer", "Co0", "Toronto, ON", "https://example.com/0")]
    db = _BudgetDb()
    sent = find_and_message_employees(driver=object(), db=db, jobs=jobs, max_to_send=0)
    assert sent == 0
    assert db.logged == []  # no profile even viewed


def test_default_budget_is_the_daily_cap(monkeypatch):
    """With no explicit budget, the full daily cap still applies."""
    _wire(monkeypatch, contacts_per_company=3)
    monkeypatch.setattr("messenger.Config.MAX_MESSAGES_PER_DAY", 2)
    jobs = [Job(f"j{i}", "Software Engineer", f"Co{i}", "Toronto, ON",
                f"https://example.com/{i}") for i in range(5)]
    sent = find_and_message_employees(driver=object(), db=_BudgetDb(), jobs=jobs)
    assert sent == 2

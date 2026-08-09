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

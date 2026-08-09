from models import Contact, Job
from messenger import (
    _company_match_score,
    _extract_tech_from_jd,
    _pick_message,
    _send_connection_with_note,
    _select_contacts_for_outreach,
)


def test_company_matching_handles_linkedin_name_variants():
    assert _company_match_score("IBM", "IBM Canada") >= 60
    assert _company_match_score("Unity", "Unity Technologies") >= 60
    assert _company_match_score("Acme", "Totally Different") == 0


def test_message_generation_stays_under_linkedin_limit_and_uses_jd_tech(monkeypatch):
    monkeypatch.setattr("messenger.Config.YOUR_NAME", "Harsh")
    job = Job(
        "job-1",
        "Backend Developer",
        "Acme",
        "Toronto, ON",
        "https://example.com/job",
        "We use Python, React, AWS, Docker, and PostgreSQL.",
    )
    contact = Contact(
        "contact-1",
        "Jane Doe",
        "Jane",
        "https://linkedin.com/in/jane-doe",
        "Acme",
        "Software Engineer",
        "Toronto, Ontario, Canada",
    )

    assert _extract_tech_from_jd(job)
    assert len(_pick_message(contact, job)) <= 300


def test_contact_selection_filters_blocked_and_prefers_relevant_canadian_contacts():
    job = Job("job-1", "Software Engineer", "Acme", "Toronto, ON", "https://example.com/job")
    contacts = [
        Contact("1", "Good Engineer", "Good", "https://linkedin.com/in/good", "Acme", "Software Engineer", "Toronto, Ontario, Canada"),
        Contact("2", "Student Person", "Student", "https://linkedin.com/in/student", "Acme", "Computer Science Student", "Toronto, Ontario, Canada"),
        Contact("3", "Quebec Engineer", "Quebec", "https://linkedin.com/in/qc", "Acme", "Software Engineer", "Montreal, Quebec, Canada"),
        Contact("4", "Sales Person", "Sales", "https://linkedin.com/in/sales", "Acme", "Account Executive", "Toronto, Ontario, Canada"),
    ]

    picked = _select_contacts_for_outreach(
        contacts,
        "Acme",
        job,
        max_results=3,
        source_label="test",
    )

    assert [c.contact_id for c in picked] == ["1"]


def test_connected_contact_uses_direct_message_flow(monkeypatch):
    contact = Contact(
        "contact-1",
        "Jane Doe",
        "Jane",
        "https://linkedin.com/in/jane-doe",
        "Acme",
        "Software Engineer",
        "Toronto, Ontario, Canada",
    )
    calls = {"dm": 0, "connect": 0}

    class DummySession:
        def record_profile_view(self):
            pass

    class DummyDriver:
        current_url = contact.profile_url

    monkeypatch.setattr("messenger.safe_get", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("messenger.get_session", lambda: DummySession())
    monkeypatch.setattr("messenger.realistic_profile_reading", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("messenger._is_contract_employee", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("messenger._get_connection_status", lambda *_args, **_kwargs: "connected")

    def fake_dm(*_args, **_kwargs):
        calls["dm"] += 1
        return True

    def fake_connect(*_args, **_kwargs):
        calls["connect"] += 1
        return True

    monkeypatch.setattr("messenger._send_direct_message", fake_dm)
    monkeypatch.setattr("messenger._click_connect_button", fake_connect)

    assert _send_connection_with_note(DummyDriver(), contact, "hello") == "dm_sent"
    assert calls == {"dm": 1, "connect": 0}

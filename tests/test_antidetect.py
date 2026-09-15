import antidetect
from antidetect import SessionTracker, check_for_linkedin_warnings


class _RateLimitedDriver:
    """Chrome's own error page, shown when LinkedIn answers with HTTP 429."""

    current_url = "https://www.linkedin.com/jobs/search/?keywords=Software+Engineer"

    def execute_script(self, script, *_args):
        if "iframe" in script:
            return None  # the error page has no iframes
        return (
            "www.linkedin.com This page isn't working If the problem continues, "
            "contact the site owner. HTTP ERROR 429 Reload"
        )


def test_http_429_error_page_stops_the_session(monkeypatch):
    monkeypatch.setattr(antidetect, "_session", SessionTracker())

    warning, reason = check_for_linkedin_warnings(_RateLimitedDriver())

    assert warning
    assert "429" in reason
    assert not antidetect.is_session_safe()

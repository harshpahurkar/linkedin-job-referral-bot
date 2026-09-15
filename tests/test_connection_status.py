import urllib.parse

import pytest
from selenium import webdriver

from messenger import _click_connect_button, _get_connection_status

# Seen live: invite and withdraw controls name the person they act on, and a
# profile page also carries Connect/Pending controls for other people.
_PROFILE_HTML = """
<main>
  <section id="top-card">
    <h1>Conal Curran</h1>
    <a href="#">Message</a>
    {action}
  </section>
  <section id="people-you-may-know">
    {suggestion}
  </section>
</main>
"""

_CONNECT_CONAL = '<a href="#" aria-label="Invite Conal Curran to connect">Connect</a>'
_PENDING_CONAL = '<a href="#" aria-label="Pending, click to withdraw invitation sent to Conal Curran">Pending</a>'
_PENDING_OTHER = '<a href="#" aria-label="Pending, click to withdraw invitation sent to Nima Shahrokhkhani">Pending</a>'
_CONNECT_OTHER = (
    '<a href="#" aria-label="Invite Alexandre J. to connect"'
    ' onclick="this.dataset.clicked = 1; return false">Connect</a>'
)


@pytest.fixture(scope="module")
def browser():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


def _open(browser, action, suggestion):
    html = _PROFILE_HTML.format(action=action, suggestion=suggestion)
    browser.get("data:text/html;charset=utf-8," + urllib.parse.quote(html))
    return browser


def test_someone_elses_pending_invite_does_not_hide_connect(browser):
    assert _get_connection_status(_open(browser, _CONNECT_CONAL, _PENDING_OTHER), "Conal") == "connect"


def test_own_pending_invite_is_pending_despite_other_connect_buttons(browser):
    assert _get_connection_status(_open(browser, _PENDING_CONAL, _CONNECT_OTHER), "Conal") == "pending"


def test_connected_when_only_other_people_can_be_invited(browser):
    assert _get_connection_status(_open(browser, "", _CONNECT_OTHER), "Conal") == "connected"


def test_connect_click_never_invites_someone_else(browser):
    page = _open(browser, "", _CONNECT_OTHER)
    _click_connect_button(page, expected_name="Conal Curran")
    assert page.execute_script('return document.querySelector("[aria-label*=Alexandre]").dataset.clicked') is None

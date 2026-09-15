import urllib.parse

import pytest
from selenium import webdriver

from messenger import _PEOPLE_LOCKUP_JS, _extract_people_from_current_page

# A company people page with a chat bubble open. The bubble's profile card
# uses the same lockup classes as employee cards, which is how an existing
# connection leaked into every company's contact list during a live run.
_PEOPLE_HTML = """
<ul>
  <li class="org-people-profile-card">
    <div class="artdeco-entity-lockup">
      <a href="https://www.linkedin.com/in/jane-doe?miniProfileUrn=x">
        <div class="artdeco-entity-lockup__title">Jane Doe</div></a>
      <div class="artdeco-entity-lockup__subtitle">Software Engineer at Acme</div>
    </div>
  </li>
</ul>
<aside class="msg-overlay-container">
  <div class="msg-overlay-conversation-bubble">
    <ul class="msg-s-message-list-content"><li>
      <div class="msg-s-profile-card artdeco-entity-lockup">
        <a href="https://www.linkedin.com/in/ACoAAAHVaQMBkc2ammqUYAOyROR8SzS8jrIJzmY">
          <div class="artdeco-entity-lockup__title">Masud Aftab 1st degree connection</div></a>
        <div class="artdeco-entity-lockup__subtitle">Enterprise Transformation Leader</div>
      </div>
    </li></ul>
  </div>
</aside>
"""


@pytest.fixture(scope="module")
def page():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    driver.get("data:text/html;charset=utf-8," + urllib.parse.quote(_PEOPLE_HTML))
    yield driver
    driver.quit()


def test_people_page_lockups_skip_the_chat_overlay(page):
    people = page.execute_script(_PEOPLE_LOCKUP_JS)
    assert [p["name"] for p in people] == ["Jane Doe"]


def test_structural_fallback_skips_the_chat_overlay(page):
    people = _extract_people_from_current_page(page, "test")
    assert [p["link"] for p in people] == ["https://www.linkedin.com/in/jane-doe"]

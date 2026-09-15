import urllib.parse

import pytest
from selenium import webdriver

from messenger import (
    _click_msg_send,
    _click_send_button,
    _close_msg_overlay,
    _find_msg_input,
    _has_prior_messages,
)

# LinkedIn's 2026 messaging overlay as seen live: it renders inside a shadow
# root on DIV.theme--light, one bubble per open conversation, and the close
# button carries its label as text instead of aria-label.
_OVERLAY_HTML = """
<div class="theme--light" id="host"></div>
<script>
const bubble = (name, history) => `
  <div class="msg-convo-wrapper msg-overlay-conversation-bubble">
    <header>
      <h2 class="msg-overlay-bubble-header__title">${name}</h2>
      <button class="msg-overlay-bubble-header__control"
              onclick="this.closest('.msg-overlay-conversation-bubble').remove()">
        <span>Close your conversation with ${name}</span></button>
    </header>
    <ul class="msg-s-message-list-content">
      <li class="msg-s-message-list__top-of-list"></li>
      ${history ? '<li class="msg-s-message-list__event">Hi, noticed you work here</li>' : ''}
    </ul>
    <form class="msg-form">
      <div class="msg-form__contenteditable" contenteditable="true" role="textbox"
           data-owner="${name}">x</div>
      <button class="msg-form__send-button" type="submit"
              onclick="event.preventDefault(); this.dataset.clicked = '1'">Send</button>
    </form>
  </div>`;
// With no prior thread, Message opens a bubble titled "New message" (seen
// live). The recipient pill markup here is a model, not a capture.
const compose = recipient => `
  <div class="msg-convo-wrapper msg-overlay-conversation-bubble">
    <header>
      <h2 class="msg-overlay-bubble-header__title">New message</h2>
    </header>
    <div class="msg-connections-typeahead"><span class="artdeco-pill">${recipient}</span></div>
    <form class="msg-form">
      <div class="msg-form__contenteditable" contenteditable="true" role="textbox"
           data-owner="${recipient}">x</div>
      <button class="msg-form__send-button" type="submit">Send</button>
    </form>
  </div>`;
document.getElementById('host').attachShadow({mode: 'open'}).innerHTML =
  '<aside class="msg-overlay-container">'
  + bubble('Masud Aftab', true) + bubble('Jane Doe', false) + '</aside>';
</script>
"""

_COMPOSE_HTML = _OVERLAY_HTML.replace("bubble('Jane Doe', false)", "compose('ChinYin (Alex) Lin')")

_SHADOW = "document.getElementById('host').shadowRoot"


@pytest.fixture(scope="module")
def browser():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


@pytest.fixture
def overlay(browser):
    browser.get("data:text/html;charset=utf-8," + urllib.parse.quote(_OVERLAY_HTML))
    return browser


@pytest.fixture
def compose_overlay(browser):
    browser.get("data:text/html;charset=utf-8," + urllib.parse.quote(_COMPOSE_HTML))
    return browser


def test_finds_the_contacts_composer_inside_the_shadow_root(overlay):
    msg_input = _find_msg_input(overlay, "Jane")
    assert msg_input is not None
    assert msg_input.get_attribute("data-owner") == "Jane Doe"


def test_never_falls_back_to_another_persons_composer(overlay):
    assert _find_msg_input(overlay, "Priya") is None


def test_finds_the_composer_of_a_new_message_bubble_addressed_to_the_contact(compose_overlay):
    msg_input = _find_msg_input(compose_overlay, "ChinYin")
    assert msg_input is not None
    assert msg_input.get_attribute("data-owner") == "ChinYin (Alex) Lin"
    assert not _has_prior_messages(compose_overlay, msg_input)


def test_never_uses_a_new_message_bubble_addressed_to_someone_else(compose_overlay):
    assert _find_msg_input(compose_overlay, "Priya") is None


def test_send_clicks_the_button_in_the_composers_own_form(overlay):
    msg_input = _find_msg_input(overlay, "Jane")
    assert _click_msg_send(overlay, msg_input)
    clicked = overlay.execute_script(
        f"return [...{_SHADOW}.querySelectorAll('.msg-form__send-button')]"
        ".map(b => b.dataset.clicked || '')"
    )
    assert clicked == ["", "1"]


def test_detects_an_existing_conversation(overlay):
    assert _has_prior_messages(overlay, _find_msg_input(overlay, "Masud"))
    assert not _has_prior_messages(overlay, _find_msg_input(overlay, "Jane"))


def test_close_overlay_closes_every_bubble(overlay):
    _close_msg_overlay(overlay)
    remaining = overlay.execute_script(
        f"return {_SHADOW}.querySelectorAll('.msg-overlay-conversation-bubble').length"
    )
    assert remaining == 0


def test_invite_send_never_clicks_a_chat_bubble_send(overlay, monkeypatch):
    # With no invite modal open, the only Send buttons belong to chat bubbles.
    monkeypatch.setattr("messenger.human_delay", lambda *_args, **_kwargs: None)
    assert not _click_send_button(overlay)
    clicked = overlay.execute_script(
        f"return [...{_SHADOW}.querySelectorAll('.msg-form__send-button')]"
        ".map(b => b.dataset.clicked || '')"
    )
    assert clicked == ["", ""]

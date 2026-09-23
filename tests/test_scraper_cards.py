import urllib.parse

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By

from scraper import CARD_SEL, _extract_card_basics

# A job card as LinkedIn's 2026 search page renders it client side (hashed
# class names removed). Server-rendered pages wrap the same markup in
# <div data-view-name="job-search-job-card">, client-rendered ones do not.
_CARD_HTML = """
<div data-display-contents="true">
  <div role="button" tabindex="0" componentkey="job-card-component-ref-4465251321">
    <div componentkey="job-card-component-ref-4465251321">
      <figure><img alt="Apptoza Inc. logo"></figure>
      <p><span>Selected, Software Engineer</span><span aria-hidden="true">Software Engineer</span></p>
      <div><p>Apptoza Inc.</p></div>
      <p>Toronto, ON (Hybrid)</p>
      <button aria-label="Dismiss Software Engineer job"></button>
      <p><span>Posted 9 minutes ago</span><span aria-hidden="true">9 minutes ago</span></p>
    </div>
  </div>
</div>
"""


@pytest.fixture(scope="module")
def browser():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


def test_card_selector_and_extraction_read_the_2026_job_card(browser):
    browser.get("data:text/html;charset=utf-8," + urllib.parse.quote(_CARD_HTML))

    cards = browser.find_elements(By.CSS_SELECTOR, CARD_SEL)
    assert len(cards) == 1

    _job_id, title, company, location, url = _extract_card_basics(cards[0])
    assert (title, company, location, url) == (
        "Software Engineer",
        "Apptoza Inc.",
        "Toronto, ON (Hybrid)",
        "https://www.linkedin.com/jobs/view/4465251321/",
    )


# The classic search page LinkedIn still serves to this account on some days.
_CLASSIC_CARD_HTML = """
<ul>
  <li class="scaffold-layout__list-item">
    <div class="job-card-container">
      <a class="job-card-list__title--link" href="https://www.linkedin.com/jobs/view/4465251322/?refId=abc">
        <span aria-hidden="true"><strong>Backend Developer</strong></span>
      </a>
      <div class="artdeco-entity-lockup__subtitle"><span>Shopify</span></div>
      <div class="artdeco-entity-lockup__caption"><ul><li><span>Ottawa, ON (Remote)</span></li></ul></div>
    </div>
  </li>
</ul>
"""


def test_card_selector_and_extraction_read_the_classic_job_card(browser):
    browser.get("data:text/html;charset=utf-8," + urllib.parse.quote(_CLASSIC_CARD_HTML))

    cards = browser.find_elements(By.CSS_SELECTOR, CARD_SEL)
    assert len(cards) == 1

    _job_id, title, company, location, url = _extract_card_basics(cards[0])
    assert (title, company, location, url) == (
        "Backend Developer",
        "Shopify",
        "Ottawa, ON (Remote)",
        "https://www.linkedin.com/jobs/view/4465251322/",
    )

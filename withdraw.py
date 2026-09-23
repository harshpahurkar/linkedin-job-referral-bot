"""Withdraw connection invites nobody accepted within two weeks.

A long queue of unanswered invites is a spam signal to LinkedIn, so each run
clears a few of the oldest ones at a human pace.
"""
import re
import time

from selenium.common.exceptions import StaleElementReferenceException

from antidetect import check_for_linkedin_warnings, natural_scroll_pattern, safe_get
from utils import get_logger, human_delay, human_move_and_click

logger = get_logger(__name__)

SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
_UNIT_DAYS = {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}

# Each Withdraw link with its card's text, in page order (newest first).
_CARDS_JS = """
return [...document.querySelectorAll("[aria-label^='Withdraw invitation sent to']")].map(a => {
  let c = a;
  while (c.parentElement && !/\\bSent \\S/.test(c.innerText)) c = c.parentElement;
  return [a, c.innerText];
});
"""

# The confirm button of the "Withdraw invitation?" dialog, which may sit in a shadow root.
_CONFIRM_JS = """
const roots = [document, ...[...document.querySelectorAll('*')].filter(e => e.shadowRoot).map(e => e.shadowRoot)];
for (const r of roots)
  for (const b of r.querySelectorAll('dialog button, [role=dialog] button, [role=alertdialog] button'))
    if (b.innerText.trim() === 'Withdraw' && b.offsetParent) return b;
return null;
"""


def invite_age_days(card_text: str) -> int | None:
    """Days since the invite in a card was sent, or None when the card doesn't say."""
    m = re.search(r"Sent (\d+|an?) (minute|hour|day|week|month|year)s? ago", card_text)
    if not m:
        return None
    n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
    return n * _UNIT_DAYS[m.group(2)]


def withdraw_stale_invites(driver, min_days: int = 14, limit: int = 25) -> int:
    """Withdraw up to ``limit`` invites pending ``min_days`` or more. Returns how many."""
    if not safe_get(driver, SENT_URL):
        return 0
    withdrawn = stalls = loaded = 0
    while withdrawn < limit and stalls < 3:
        cards = driver.execute_script(_CARDS_JS)
        stale = [(a, age) for a, text in cards
                 if (age := invite_age_days(text)) is not None and age >= min_days]
        if not stale:
            # Old invites sit at the bottom; scrolling to the end loads the next batch.
            stalls = stalls + 1 if len(cards) == loaded else 0
            loaded = len(cards)
            if cards:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'end', behavior: 'smooth'});", cards[-1][0])
            natural_scroll_pattern(driver, scrolls=2)
            human_delay(1.5, 3)
            continue

        link, age = stale[0]
        name = link.get_attribute("aria-label").removeprefix("Withdraw invitation sent to").strip()
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', behavior: 'smooth'});", link)
        human_delay(0.8, 2)
        human_move_and_click(driver, link)
        confirm = None
        for _ in range(10):
            time.sleep(0.5)
            confirm = driver.execute_script(_CONFIRM_JS)
            if confirm:
                break
        if not confirm:
            logger.warning(f"  Withdraw dialog never showed for {name}. Stopping.")
            break
        human_delay(0.6, 1.5)
        human_move_and_click(driver, confirm)
        human_delay(4, 10)
        try:
            link.is_enabled()  # still on the page means the withdraw didn't go through
            logger.warning(f"  Invite to {name} is still listed after withdrawing. Stopping.")
            break
        except StaleElementReferenceException:
            pass
        withdrawn += 1
        logger.info(f"  ↩️  Withdrew invite to {name} ({age} days old)")

        warning, reason = check_for_linkedin_warnings(driver)
        if warning:
            logger.critical(f"🛑 LinkedIn warning while withdrawing: {reason}")
            break
    logger.info(f"↩️  Withdrew {withdrawn} invite(s) older than {min_days} days.")
    return withdrawn

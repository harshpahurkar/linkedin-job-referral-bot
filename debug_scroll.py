"""
Quick debug script — load one LinkedIn search page and figure out
what the actual DOM looks like so we can fix the scroll/card selectors.
"""
import time, json
from utils import create_driver, get_logger, human_delay
from auth import login
from config import Config

logger = get_logger("debug")

driver = create_driver()
if not login(driver):
    print("Login failed"); driver.quit(); exit()

# Load a search
driver.get("https://www.linkedin.com/jobs/search/?keywords=Full-Stack+Developer&location=Canada")
time.sleep(5)

# Dump info via JS
info = driver.execute_script("""
    // 1. How many cards with various selectors
    const selectors = {
        '.jobs-search-results__list-item': document.querySelectorAll('.jobs-search-results__list-item').length,
        '.job-card-container': document.querySelectorAll('.job-card-container').length,
        '.job-card-list': document.querySelectorAll('.job-card-list').length,
        'li.ember-view.occludable-update': document.querySelectorAll('li.ember-view.occludable-update').length,
        '.scaffold-layout__list-item': document.querySelectorAll('.scaffold-layout__list-item').length,
        'div[data-job-id]': document.querySelectorAll('div[data-job-id]').length,
        'a[data-job-id]': document.querySelectorAll('a[data-job-id]').length,
        'li.jobs-search-results__list-item': document.querySelectorAll('li.jobs-search-results__list-item').length,
    };

    // 2. Check scrollable containers
    const containers = {};
    ['.jobs-search-results-list', '.scaffold-layout__list',
     '.jobs-search-two-pane__wrapper', '.scaffold-layout__list-container',
     '.jobs-search-results-list__container'].forEach(sel => {
        const el = document.querySelector(sel);
        if (el) {
            containers[sel] = {
                scrollHeight: el.scrollHeight,
                clientHeight: el.clientHeight,
                scrollTop: el.scrollTop,
                overflow: getComputedStyle(el).overflow,
                overflowY: getComputedStyle(el).overflowY,
            };
        } else {
            containers[sel] = null;
        }
    });

    // 3. Find what's actually scrollable by walking up from first card
    let scrollParent = null;
    const firstCard = document.querySelector('.job-card-container') ||
                      document.querySelector('.jobs-search-results__list-item');
    if (firstCard) {
        let el = firstCard.parentElement;
        while (el && el !== document.body) {
            const style = getComputedStyle(el);
            if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                el.scrollHeight > el.clientHeight) {
                scrollParent = {
                    tag: el.tagName,
                    className: el.className.substring(0, 120),
                    scrollHeight: el.scrollHeight,
                    clientHeight: el.clientHeight,
                };
                break;
            }
            el = el.parentElement;
        }
    }

    // 4. Get the UL/OL that holds job cards
    let listParent = null;
    if (firstCard) {
        const p = firstCard.closest('ul, ol, div.scaffold-layout__list-container');
        if (p) {
            listParent = {
                tag: p.tagName,
                className: p.className.substring(0, 120),
                childCount: p.children.length,
            };
        }
    }

    return {selectors, containers, scrollParent, listParent};
""")

print("\n" + "="*60)
print(json.dumps(info, indent=2))
print("="*60)

# Now try scrolling the actual scroll parent and see if more cards load
scroll_js = """
    const firstCard = document.querySelector('.job-card-container') ||
                      document.querySelector('.jobs-search-results__list-item');
    if (!firstCard) return 0;
    let el = firstCard.parentElement;
    while (el && el !== document.body) {
        const style = getComputedStyle(el);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
            el.scrollHeight > el.clientHeight) {
            el.scrollTop += 800;
            return el.scrollTop;
        }
        el = el.parentElement;
    }
    window.scrollBy(0, 800);
    return window.scrollY;
"""

print("\nScrolling 10 times using the real scroll parent...")
for i in range(10):
    pos = driver.execute_script(scroll_js)
    time.sleep(0.5)
    cards = driver.find_elements("css selector", ".jobs-search-results__list-item, .job-card-container")
    print(f"  Scroll {i+1}: scrollPos={pos}, cards={len(cards)}")

driver.save_screenshot("c:\\Users\\Harsh\\Desktop\\Projects\\linkedin-job-referral-bot\\data\\debug_screenshot.png")
print("\nScreenshot saved.")
driver.quit()

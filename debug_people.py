"""
Debug script — inspect a company /people/ page to find the right selectors.
"""
import time, json
from utils import create_driver, get_logger, human_delay
from auth import login

logger = get_logger("debug")

driver = create_driver()
if not login(driver):
    print("Login failed"); driver.quit(); exit()

# Search for a real company from our scraped jobs
company = "Reddit"
search_url = (
    f"https://www.linkedin.com/search/results/companies/"
    f"?keywords={company}&origin=GLOBAL_SEARCH_HEADER"
)
driver.get(search_url)
time.sleep(5)

# Click first company result to get company URL
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    first_result = WebDriverWait(driver, 8).until(
        EC.element_to_be_clickable(
            (By.CSS_SELECTOR, ".reusable-search__result-container a.app-aware-link")
        )
    )
    company_url = (first_result.get_attribute("href") or "").split("?")[0]
    print(f"Company URL: {company_url}")
except:
    print("No company found, trying direct URL")
    company_url = "https://www.linkedin.com/company/reddit-com"

# Go to /people/ page
people_url = company_url.rstrip("/") + "/people/"
print(f"People URL: {people_url}")
driver.get(people_url)
time.sleep(5)

# Scroll down a bit to load cards
for i in range(5):
    driver.execute_script("window.scrollBy(0, 600);")
    time.sleep(0.5)

# Dump all the selectors we can find
info = driver.execute_script("""
    const selectors = {
        '.org-people-profile-card': document.querySelectorAll('.org-people-profile-card').length,
        '.artdeco-card .scaffold-layout__list-item': document.querySelectorAll('.artdeco-card .scaffold-layout__list-item').length,
        '.scaffold-finite-scroll__content > li': document.querySelectorAll('.scaffold-finite-scroll__content > li').length,
        '.org-people__card-container': document.querySelectorAll('.org-people__card-container').length,
        'div[data-view-name="org-people-profile-card"]': document.querySelectorAll('div[data-view-name="org-people-profile-card"]').length,
        '.artdeco-entity-lockup': document.querySelectorAll('.artdeco-entity-lockup').length,
        '.artdeco-list__item': document.querySelectorAll('.artdeco-list__item').length,
        'section.org-people': document.querySelectorAll('section.org-people').length,
    };

    // Find the actual card elements and inspect their structure
    const cards = document.querySelectorAll('.artdeco-entity-lockup, .org-people-profile-card');
    const cardDetails = [];
    for (let i = 0; i < Math.min(cards.length, 3); i++) {
        const card = cards[i];
        cardDetails.push({
            tag: card.tagName,
            className: card.className.substring(0, 150),
            innerHTML: card.innerHTML.substring(0, 500),
            outerHTML: card.outerHTML.substring(0, 300),
        });
    }

    // Check for a list container
    const containers = {};
    ['.scaffold-finite-scroll__content',
     '.org-people__card-container',
     '.org-grid__content-container'].forEach(sel => {
        const el = document.querySelector(sel);
        containers[sel] = el ? {children: el.children.length, tag: el.tagName} : null;
    });

    // Try broader: anything with 'people' or 'profile' in it
    const peopleDivs = document.querySelectorAll('[class*="people"]');
    const peopleInfo = [];
    for (const el of peopleDivs) {
        if (el.children.length > 2) {
            peopleInfo.push({
                tag: el.tagName,
                className: el.className.substring(0, 120),
                childCount: el.children.length,
            });
        }
    }

    return {selectors, containers, cardDetails, peopleDivs: peopleInfo};
""")

print("\n" + "="*60)
print(json.dumps(info, indent=2, default=str))
print("="*60)

# Also try to extract a person's name/title/link from whatever we find
people = driver.execute_script("""
    // Try various selectors to find person cards
    const results = [];

    // Strategy 1: artdeco-entity-lockup (common on people pages)
    document.querySelectorAll('.artdeco-entity-lockup').forEach(card => {
        const name = card.querySelector('.artdeco-entity-lockup__title')?.textContent?.trim() || '';
        const subtitle = card.querySelector('.artdeco-entity-lockup__subtitle')?.textContent?.trim() || '';
        const link = card.querySelector('a[href*="/in/"]')?.href || '';
        if (name) results.push({name, subtitle, link, source: 'lockup'});
    });

    // Strategy 2: org-people-profile-card
    document.querySelectorAll('.org-people-profile-card').forEach(card => {
        const name = card.querySelector('.org-people-profile-card__profile-title')?.textContent?.trim() || '';
        const subtitle = card.querySelector('.lt-line-clamp, .artdeco-entity-lockup__subtitle')?.textContent?.trim() || '';
        const link = card.querySelector('a[href*="/in/"]')?.href || '';
        if (name) results.push({name, subtitle, link, source: 'org-card'});
    });

    // Strategy 3: Any link to /in/ profiles with nearby text
    document.querySelectorAll('a[href*="/in/"]').forEach(a => {
        const name = a.textContent?.trim() || '';
        const parent = a.closest('li, div.artdeco-entity-lockup, div[class*="card"]');
        const subtitle = parent?.querySelector('[class*="subtitle"], [class*="caption"]')?.textContent?.trim() || '';
        if (name && name.length > 2 && name.length < 50) {
            results.push({name, subtitle, link: a.href, source: 'a-tag'});
        }
    });

    return results;
""")

print(f"\nFound {len(people)} people:")
for p in people[:10]:
    print(f"  [{p['source']}] {p['name']} — {p['subtitle'][:60]} — {p['link'][:60]}")

driver.save_screenshot("c:\\Users\\Harsh\\Desktop\\Projects\\linkedin-job-referral-bot\\data\\debug_people.png")
print("\nScreenshot saved.")
driver.quit()

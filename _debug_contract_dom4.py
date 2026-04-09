"""Debug v4: aggressive scroll + click 'Show all experience' to find employment type."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get
from selenium.webdriver.common.by import By

logger = get_logger("debug_contract4")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(2)

safe_get(driver, "https://www.linkedin.com/in/mukuliskul/")
time.sleep(5)

# Aggressive scroll: go all the way to the bottom in small steps
page_height = driver.execute_script("return document.body.scrollHeight;")
print(f"Initial page height: {page_height}")

for i in range(20):
    driver.execute_script(f"window.scrollTo(0, {300 * (i+1)});")
    time.sleep(0.5)

time.sleep(2)

new_height = driver.execute_script("return document.body.scrollHeight;")
print(f"After scroll page height: {new_height}")

# Scroll back to top slowly
for i in range(10):
    driver.execute_script(f"window.scrollTo(0, {new_height - 300 * (i+1)});")
    time.sleep(0.3)

time.sleep(1)

# Get FULL page text now
page_text = driver.execute_script("return document.body.innerText;")
lines = page_text.split('\n')

print("\n" + "="*80)
print("KEYWORD MATCHES IN FULL PAGE")
print("="*80)
for i, line in enumerate(lines):
    line = line.strip()
    if not line:
        continue
    lower = line.lower()
    if any(kw in lower for kw in ['contract', 'full-time', 'full time', 'part-time', 
                                    'part time', 'freelance', 'experience',
                                    'employment type', 'contract full']):
        print(f"  LINE {i}: {line[:150]}")

# Find "Experience" and dump 80 lines after it
print("\n" + "="*80)
print("EXPERIENCE SECTION + 80 LINES")
print("="*80)
found = False
for i, line in enumerate(lines):
    if 'Experience' == line.strip():
        found = True
        start = max(0, i-2)
        end = min(len(lines), i+80)
        for j in range(start, end):
            l = lines[j].strip()
            if l:
                flag = ""
                if any(kw in l.lower() for kw in ['contract', 'full-time', 'full time', 'part-time']):
                    flag = " <<<<< MATCH"
                print(f"  LINE {j}: {l[:150]}{flag}")
        break

if not found:
    print("  'Experience' heading NOT found in page text")
    # Try partial match
    for i, line in enumerate(lines):
        if 'xperience' in line.strip():
            print(f"  Partial match LINE {i}: {line.strip()[:100]}")

# Also check page source for aria-labels or data attributes
aria_dump = driver.execute_script(r"""
    const results = [];
    const els = document.querySelectorAll('[aria-label], [data-section]');
    for (const el of els) {
        const label = el.getAttribute('aria-label') || el.getAttribute('data-section') || '';
        if (/experience/i.test(label)) {
            results.push('aria/data: "' + label + '" tag=' + el.tagName + ' id=' + (el.id || 'none'));
        }
    }
    return results;
""")
print("\n--- ARIA/DATA ATTRIBUTES ---")
for a in aria_dump:
    print(f"  {a}")

print("\nBrowser stays open 20s...")
time.sleep(20)
driver.quit()

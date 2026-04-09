"""Debug v3: dump raw page text to find employment type."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get

logger = get_logger("debug_contract3")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(2)

safe_get(driver, "https://www.linkedin.com/in/mukuliskul/")
time.sleep(5)

# Scroll ALL the way down to force lazy-load everything
for i in range(10):
    driver.execute_script(f"window.scrollTo(0, {500 * (i+1)});")
    time.sleep(0.8)

# Scroll back to top
driver.execute_script("window.scrollTo(0, 0);")
time.sleep(1)

# Get full page text
page_text = driver.execute_script("return document.body.innerText;")

print("\n" + "="*80)
print("FULL PAGE TEXT (searching for contract/full-time)")
print("="*80)

lines = page_text.split('\n')
for i, line in enumerate(lines):
    line = line.strip()
    if not line:
        continue
    lower = line.lower()
    if any(kw in lower for kw in ['contract', 'full-time', 'full time', 'part-time', 'part time',
                                    'freelance', 'experience', 'mpac', 'seneca', 'developer',
                                    'systems', 'senior', 'valedictorian']):
        print(f"  LINE {i}: {line[:120]}")

print("\n" + "="*80)
print("ALL LINES (around experience area)")
print("="*80)
# Find "Experience" heading and dump surrounding context
for i, line in enumerate(lines):
    if 'xperience' in line:
        start = max(0, i-2)
        end = min(len(lines), i+50)
        for j in range(start, end):
            l = lines[j].strip()
            if l:
                flag = ""
                if any(kw in l.lower() for kw in ['contract', 'full-time', 'full time', 'part-time']):
                    flag = " <<<<< MATCH"
                print(f"  LINE {j}: {l[:120]}{flag}")
        break

print("\nBrowser stays open 20s...")
time.sleep(20)
driver.quit()

"""Debug v5: check URL, wait longer, try innerHTML search for experience."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get
from selenium.webdriver.common.by import By

logger = get_logger("debug_contract5")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(3)

# Navigate directly
print("Navigating to profile...")
driver.get("https://www.linkedin.com/in/mukuliskul/")
time.sleep(8)  # Wait longer

print(f"Current URL: {driver.current_url}")
print(f"Title: {driver.title}")
print(f"Page height: {driver.execute_script('return document.body.scrollHeight')}")
print(f"Document height: {driver.execute_script('return document.documentElement.scrollHeight')}")

# Check for scrollable main container (LinkedIn uses overflow scrolling)
scroll_containers = driver.execute_script(r"""
    const results = [];
    const all = document.querySelectorAll('*');
    for (const el of all) {
        const style = window.getComputedStyle(el);
        if ((style.overflow === 'auto' || style.overflow === 'scroll' || 
             style.overflowY === 'auto' || style.overflowY === 'scroll') &&
            el.scrollHeight > el.clientHeight + 100) {
            const tag = el.tagName.toLowerCase();
            const cls = (el.className || '').toString().substring(0, 60);
            const id = el.id || 'none';
            results.push({
                tag: tag,
                cls: cls,
                id: id,
                scrollHeight: el.scrollHeight,
                clientHeight: el.clientHeight,
                overflow: style.overflow + '/' + style.overflowY
            });
        }
    }
    return results;
""")

print(f"\nScrollable containers found: {len(scroll_containers)}")
for sc in scroll_containers:
    print(f"  <{sc['tag']}> id={sc['id']} cls={sc['cls'][:50]} scrollH={sc['scrollHeight']} clientH={sc['clientHeight']} overflow={sc['overflow']}")

# If we found scrollable containers, scroll the main one
if scroll_containers:
    # Pick the tallest one
    main_container = max(scroll_containers, key=lambda x: x['scrollHeight'])
    print(f"\nScrolling main container: <{main_container['tag']}> scrollHeight={main_container['scrollHeight']}")
    
    # Scroll the container using JS
    for i in range(20):
        driver.execute_script(f"""
            const containers = document.querySelectorAll('*');
            for (const el of containers) {{
                const style = window.getComputedStyle(el);
                if ((style.overflow === 'auto' || style.overflow === 'scroll' ||
                     style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 100) {{
                    el.scrollTop = {400 * (i+1)};
                    break;
                }}
            }}
        """)
        time.sleep(0.5)
    
    time.sleep(2)

# Now get the full text from the scrollable container
full_text = driver.execute_script(r"""
    // Find the main scrollable container
    const all = document.querySelectorAll('*');
    let mainContainer = document.body;
    let maxScroll = 0;
    for (const el of all) {
        const style = window.getComputedStyle(el);
        if ((style.overflow === 'auto' || style.overflow === 'scroll' ||
             style.overflowY === 'auto' || style.overflowY === 'scroll') &&
            el.scrollHeight > maxScroll) {
            maxScroll = el.scrollHeight;
            mainContainer = el;
        }
    }
    return mainContainer.innerText;
""")

lines = full_text.split('\n')
print(f"\nTotal lines in scrollable container: {len(lines)}")

print("\n" + "="*80)
print("KEYWORD MATCHES")
print("="*80)
for i, line in enumerate(lines):
    line = line.strip()
    if not line:
        continue
    lower = line.lower()
    if any(kw in lower for kw in ['contract', 'full-time', 'full time', 'part-time',
                                    'experience', 'mpac', 'municipal', 'seneca',
                                    'employment']):
        print(f"  LINE {i}: {line[:150]}")

# Dump around "Experience" heading
print("\n" + "="*80)
print("EXPERIENCE SECTION CONTEXT")
print("="*80)
for i, line in enumerate(lines):
    if line.strip() == 'Experience':
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

# Also search raw HTML source for "Contract" and "Full-time"
page_source = driver.page_source
for keyword in ['Contract', 'Full-time', 'contract', 'full-time', 'Contract Full-time']:
    idx = page_source.find(keyword)
    if idx >= 0:
        context = page_source[max(0,idx-100):idx+100]
        print(f"\nHTML source contains '{keyword}' at position {idx}:")
        print(f"  ...{context}...")

print("\nBrowser stays open 30s...")
time.sleep(30)
driver.quit()

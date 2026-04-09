"""Debug v2: scroll to experience and dump employment type text from Mukul's profile."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get, get_session

logger = get_logger("debug_contract2")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(2)

safe_get(driver, "https://www.linkedin.com/in/mukuliskul/")
time.sleep(4)

# Scroll down gradually to load experience section
for i in range(5):
    driver.execute_script(f"window.scrollTo(0, {400 * (i+1)});")
    time.sleep(1)

# Now dump the full page looking for experience-related elements
dump = driver.execute_script(r"""
    const results = [];
    
    // 1. Find ALL section-like containers and their headings
    const sections = document.querySelectorAll('section, div[id]');
    for (const sec of sections) {
        const id = sec.id || '';
        // Check h2/h3 inside
        const headings = sec.querySelectorAll('h2, h3');
        for (const h of headings) {
            const hText = (h.textContent || '').trim().substring(0, 50);
            if (/experience/i.test(hText) || /experience/i.test(id)) {
                results.push('=== EXPERIENCE SECTION FOUND ===');
                results.push('  tag=' + sec.tagName + ' id="' + id + '" heading="' + hText + '"');
                
                // Dump ALL text in this section
                const allEls = sec.querySelectorAll('*');
                let count = 0;
                for (const el of allEls) {
                    if (count > 150) break;
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length > 100) continue;
                    if (el.children && el.children.length > 5) continue;
                    
                    const tag = el.tagName.toLowerCase();
                    const cls = (el.className || '').toString().substring(0, 40);
                    const lower = text.toLowerCase();
                    
                    let flag = '';
                    if (/contract|full.?time|part.?time|freelance|intern/i.test(lower)) {
                        flag = ' <<<<< MATCH';
                    }
                    
                    results.push('  <' + tag + '> cls="' + cls + '" => "' + text.substring(0, 80) + '"' + flag);
                    count++;
                }
            }
        }
    }
    
    if (results.length === 0) {
        results.push('NO EXPERIENCE SECTION FOUND BY HEADING SEARCH');
        // Fallback: search entire page for contract/full-time text
        results.push('--- Searching entire page for employment type text ---');
        const all = document.querySelectorAll('span, div, p');
        let count = 0;
        for (const el of all) {
            if (count > 30) break;
            const text = (el.innerText || el.textContent || '').trim();
            if (!text || text.length > 100) continue;
            if (el.children && el.children.length > 3) continue;
            const lower = text.toLowerCase();
            if (/contract|full.?time|part.?time/i.test(lower)) {
                const tag = el.tagName.toLowerCase();
                const cls = (el.className || '').toString().substring(0, 40);
                results.push('  <' + tag + '> cls="' + cls + '" => "' + text.substring(0, 80) + '"');
                count++;
            }
        }
    }
    
    return results;
""")

print("\n" + "="*80)
print("EXPERIENCE SECTION DOM DUMP v2")
print("="*80)
for line in dump:
    print(line)

print("\n\nBrowser stays open 30s...")
time.sleep(30)
driver.quit()

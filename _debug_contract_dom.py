"""Debug: visit a known contract profile and dump the experience section DOM."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get, get_session

logger = get_logger("debug_contract")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(2)

safe_get(driver, "https://www.linkedin.com/in/mukuliskul/")
time.sleep(3)

# Scroll to experience
driver.execute_script("window.scrollTo(0, 600);")
time.sleep(2)

# Dump all short text spans in the experience section area
dump = driver.execute_script(r"""
    const results = [];
    
    // Try multiple selectors for the experience section
    const selectors = [
        '#experience',
        '[id*="experience"]',
        'section.experience',
        '[data-field="experience_grouping"]',
    ];
    
    let expSection = null;
    for (const sel of selectors) {
        expSection = document.querySelector(sel);
        if (expSection) {
            results.push('FOUND experience section via: ' + sel);
            break;
        }
    }
    
    if (!expSection) {
        results.push('NO experience section found, falling back to body');
        // Try finding by text content
        const allSections = document.querySelectorAll('section');
        for (const sec of allSections) {
            const h2 = sec.querySelector('h2');
            if (h2 && /experience/i.test(h2.textContent)) {
                expSection = sec;
                results.push('Found via section>h2 text: ' + h2.textContent.trim());
                break;
            }
        }
        if (!expSection) {
            // Last resort: look for the profile section with experience
            const divs = document.querySelectorAll('div[id]');
            for (const d of divs) {
                if (/experience/i.test(d.id)) {
                    expSection = d;
                    results.push('Found via div#id: ' + d.id);
                    break;
                }
            }
        }
        if (!expSection) expSection = document.body;
    }
    
    const sectionRect = expSection.getBoundingClientRect();
    results.push('Section rect: top=' + sectionRect.top + ' height=' + sectionRect.height);
    
    // Get ALL visible text elements in the experience section
    const els = expSection.querySelectorAll('span, div, p, li, h3, h4, a');
    let count = 0;
    for (const el of els) {
        if (count > 100) break;
        const text = (el.innerText || el.textContent || '').trim();
        if (!text || text.length > 80) continue;
        // Skip if it has many children (container element)
        if (el.children && el.children.length > 5) continue;
        
        const rect = el.getBoundingClientRect();
        const relTop = Math.round(rect.top - sectionRect.top);
        const tag = el.tagName.toLowerCase();
        const cls = (el.className || '').toString().substring(0, 60);
        
        // Highlight contract-related text
        const lower = text.toLowerCase();
        let flag = '';
        if (/\b(contract|contractor|freelance|part.?time|internship|temporary|full.?time)\b/.test(lower)) {
            flag = ' <<<< MATCH';
        }
        
        results.push(
            'relTop=' + relTop + ' <' + tag + '> cls="' + cls + '" text="' + text.substring(0, 70) + '"' + flag
        );
        count++;
    }
    
    return results;
""")

print("\n" + "="*80)
print("EXPERIENCE SECTION DOM DUMP")
print("="*80)
for line in dump:
    print(line)

print("\n\nBrowser stays open 30s so you can inspect...")
time.sleep(30)
driver.quit()

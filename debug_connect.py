"""Debug: Inspect Connect button on a LinkedIn profile (with proper login)."""
import time, json, sys, os
sys.stdout.reconfigure(encoding='utf-8')

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from auth import login
from utils import create_driver

# Two profiles to test
PROFILES = [
    "https://www.linkedin.com/in/williamgranados/",
    "https://www.linkedin.com/in/duclc/",
]

driver = create_driver()
out = []

try:
    # Log in properly using auth module
    out.append("Logging in...")
    logged_in = login(driver)
    out.append(f"Login result: {logged_in}")
    if not logged_in:
        out.append("FAILED TO LOG IN - aborting")
        result = "\n".join(out)
        with open(os.path.join(os.path.dirname(__file__), "data", "debug_connect_output.txt"), "w", encoding="utf-8") as f:
            f.write(result)
        print(result)
        driver.quit()
        sys.exit(1)

    for profile_url in PROFILES:
        out.append(f"\n{'='*60}")
        out.append(f"PROFILE: {profile_url}")
        out.append(f"{'='*60}")

        driver.get(profile_url)
        time.sleep(5)
        out.append(f"Current URL: {driver.current_url}")
        out.append(f"Title: {driver.title}")

        data = driver.execute_script("""
            const results = [];

            // 1. ALL buttons with any interesting text
            document.querySelectorAll('button').forEach((btn, i) => {
                const text = btn.textContent.trim().replace(/\\s+/g, ' ').substring(0, 100);
                const label = btn.getAttribute('aria-label') || '';
                const cls = btn.className.substring(0, 150);
                const visible = btn.offsetParent !== null;
                const rect = btn.getBoundingClientRect();

                const interesting = ['connect', 'more', 'follow', 'message', 'pending',
                                     'withdraw', 'accept', 'ignore', 'send', 'invite'];
                const isInteresting = interesting.some(w =>
                    text.toLowerCase().includes(w) ||
                    label.toLowerCase().includes(w)
                ) || cls.includes('pvs-profile') || cls.includes('artdeco-button--primary')
                  || cls.includes('artdeco-button--secondary');

                if (isInteresting) {
                    results.push({
                        i: i,
                        text: text,
                        ariaLabel: label,
                        class: cls,
                        visible: visible,
                        top: Math.round(rect.top),
                        width: Math.round(rect.width),
                        disabled: btn.disabled,
                    });
                }
            });

            // 2. Any leaf element with exact text "Connect"
            results.push('--- LEAF ELEMENTS WITH CONNECT ---');
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length === 0 && el.textContent.trim() === 'Connect') {
                    const p = el.parentElement;
                    const gp = p ? p.parentElement : null;
                    results.push({
                        tag: el.tagName,
                        class: el.className.substring(0, 80),
                        parentTag: p ? p.tagName : 'none',
                        parentClass: p ? p.className.substring(0, 80) : '',
                        parentLabel: p ? (p.getAttribute('aria-label') || '') : '',
                        gpTag: gp ? gp.tagName : 'none',
                        gpClass: gp ? gp.className.substring(0, 80) : '',
                        visible: el.offsetParent !== null,
                    });
                }
            });

            // 3. Connection status
            results.push('--- CONNECTION STATUS ---');
            const bodyText = document.body.innerText.substring(0, 5000);
            results.push({
                hasConnect: bodyText.includes('Connect'),
                hasMessage: bodyText.includes('Message'),
                hasPending: bodyText.includes('Pending'),
                hasFollowing: bodyText.includes('Following'),
                hasMoreActions: !!document.querySelector("button[aria-label*='More actions']"),
                hasMoreActionsI: !!document.querySelector("button[aria-label*='more actions']"),
            });

            // 4. Now try clicking More if it exists, and see what's in the dropdown
            const moreBtn = document.querySelector("button[aria-label*='More actions'], button[aria-label*='more actions']");
            if (moreBtn) {
                moreBtn.click();
                results.push('--- CLICKED MORE - DROPDOWN ITEMS ---');
                // Small delay handled by sync check
                const items = document.querySelectorAll('div[role="menu"] li, div.artdeco-dropdown__content li');
                items.forEach((item, j) => {
                    results.push({
                        j: j,
                        text: item.textContent.trim().replace(/\\s+/g, ' ').substring(0, 80),
                        class: item.className.substring(0, 80),
                    });
                });
            }

            return results;
        """)

        out.append(json.dumps(data, indent=2, default=str))

        # If we clicked More, wait a bit and check dropdown again
        time.sleep(1)
        dropdown_data = driver.execute_script("""
            const items = document.querySelectorAll('div[role="menu"] li, div.artdeco-dropdown__content li');
            const results = [];
            items.forEach((item, j) => {
                results.push({
                    j: j,
                    text: item.textContent.trim().replace(/\\s+/g, ' ').substring(0, 80),
                    html: item.innerHTML.substring(0, 200),
                });
            });
            return results;
        """)
        if dropdown_data:
            out.append("--- DROPDOWN AFTER DELAY ---")
            out.append(json.dumps(dropdown_data, indent=2, default=str))

    result = "\n".join(out)
    outpath = os.path.join(os.path.dirname(__file__), "data", "debug_connect_output.txt")
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(result)
    print(result)

finally:
    driver.quit()

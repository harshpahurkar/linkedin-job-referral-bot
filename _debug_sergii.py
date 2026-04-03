"""
Live debug: navigate to Sergii Sergunin's profile and screenshot every step
of the Connect flow to find what's going wrong.
"""

import os
import time
import traceback
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains

from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session

logger = get_logger("debug_sergii")

SCREENSHOTS_DIR = os.path.join("data", "screenshots", "debug_sergii")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

PROFILE_URL = "https://www.linkedin.com/in/kluge"

def screenshot(driver, name):
    """Save a screenshot with a sequential prefix."""
    ts = datetime.now().strftime("%H%M%S")
    path = os.path.join(SCREENSHOTS_DIR, f"{ts}_{name}.png")
    driver.save_screenshot(path)
    logger.info(f"  📸 {name} → {path}")
    return path


def main():
    Config.HEADLESS = False
    Config.LOG_LEVEL = "DEBUG"

    driver = None
    try:
        logger.info("=" * 60)
        logger.info("🔬 LIVE DEBUG — Sergii Sergunin's profile")
        logger.info("=" * 60)

        driver = create_driver()
        reset_session()

        logger.info("🔐 Logging in …")
        if not login(driver):
            logger.error("❌ Login failed")
            return

        logger.info("✅ Logged in")
        time.sleep(2)

        # ── Step 1: Navigate to profile ──────────────────────────
        logger.info(f"🌐 Navigating to {PROFILE_URL}")
        driver.get(PROFILE_URL)
        time.sleep(3)
        screenshot(driver, "01_profile_loaded")
        logger.info(f"  URL: {driver.current_url}")

        # ── Step 2: Scroll to top ───────────────────────────────
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
        screenshot(driver, "02_scrolled_top")

        # ── Step 3: Analyze all buttons / links on the page ─────
        vw = driver.execute_script("return window.innerWidth;")
        vh = driver.execute_script("return window.innerHeight;")
        logger.info(f"  Viewport: {vw}x{vh}")

        # Find ALL elements that contain "connect" (case insensitive)
        candidates = driver.find_elements(
            By.XPATH,
            "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
            " | //a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
            " | //*[@role='button'][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'), 'connect')]"
        )
        logger.info(f"  Found {len(candidates)} elements containing 'connect':")
        for idx, btn in enumerate(candidates[:15]):
            try:
                tag = btn.tag_name
                text = (btn.text or "").strip().replace("\n", " ")[:60]
                label = (btn.get_attribute("aria-label") or "")[:60]
                href = (btn.get_attribute("href") or "")[:80]
                displayed = btn.is_displayed()
                loc = btn.location
                size = btn.size
                parent_tag = driver.execute_script("return arguments[0].parentElement?.tagName || 'none';", btn)
                parent_class = driver.execute_script("return (arguments[0].parentElement?.className || '').substring(0,50);", btn)
                logger.info(
                    f"    [{idx}] <{tag}> text='{text}' "
                    f"label='{label}' displayed={displayed} "
                    f"loc=({loc['x']},{loc['y']}) size={size['width']}x{size['height']} "
                    f"href='{href}' parent=<{parent_tag} class='{parent_class}'>"
                )
            except Exception as e:
                logger.info(f"    [{idx}] ERROR reading: {e}")

        # ── Step 4: Highlight the CORRECT Connect button ─────────
        # The real Connect button should have aria-label like "Invite ... to connect"
        real_connect = None
        for btn in candidates:
            try:
                if not btn.is_displayed():
                    continue
                label = (btn.get_attribute("aria-label") or "").lower()
                text = (btn.text or "").strip().lower()
                # Must have "invite" in label or text be exactly "connect"
                if "invite" in label and "connect" in label:
                    real_connect = btn
                    logger.info(f"  ✅ REAL Connect button found by aria-label: '{label}'")
                    break
                if text == "connect":
                    real_connect = btn
                    logger.info(f"  ✅ REAL Connect button found by exact text: '{text}', label='{label}'")
                    break
            except Exception:
                continue

        # Also highlight what the current code picks
        # (first element that passes the existing filter)
        current_pick = None
        for btn in candidates:
            try:
                if not btn.is_displayed():
                    continue
                text = (btn.text or "").strip().lower()
                label = (btn.get_attribute("aria-label") or "").lower()
                if any(x in text for x in ["disconnect", "connections", "connected"]):
                    continue
                if any(x in label for x in ["disconnect", "connections", "connected"]):
                    continue
                loc = btn.location
                size = btn.size
                if loc.get("x", 0) + size.get("width", 0) / 2 >= vw * 0.72:
                    continue
                try:
                    parent_classes = driver.execute_script(
                        "return arguments[0].closest("
                        "'div.artdeco-dropdown__content, [role=\"menu\"],"
                        " .artdeco-popover__content') !== null;", btn)
                    if parent_classes:
                        continue
                except Exception:
                    pass
                if loc.get("y", 0) > 650:
                    continue
                current_pick = btn
                current_text = text
                current_label = label
                break
            except Exception:
                continue

        if current_pick:
            logger.info(f"  🟡 Current code would pick: text='{current_text}', label='{current_label}'")
            # Highlight it yellow
            driver.execute_script(
                "arguments[0].style.outline='4px solid yellow'; arguments[0].style.outlineOffset='2px';",
                current_pick
            )

        if real_connect and real_connect != current_pick:
            logger.info("  🔴 MISMATCH: current code picks the WRONG element!")
            # Highlight real one green
            driver.execute_script(
                "arguments[0].style.outline='4px solid lime'; arguments[0].style.outlineOffset='2px';",
                real_connect
            )
        elif real_connect:
            logger.info("  🟢 Current code picks the correct element")

        screenshot(driver, "03_buttons_highlighted")

        # ── Step 5: Try clicking the REAL Connect button ─────────
        if real_connect:
            logger.info("  🖱️ Clicking the REAL Connect button …")
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", real_connect)
                time.sleep(0.3)
                ActionChains(driver).move_to_element(real_connect).pause(0.2).click().perform()
                logger.info("  ✅ Clicked via ActionChains")
            except Exception as e:
                logger.info(f"  ActionChains failed: {e}, trying JS click")
                driver.execute_script("arguments[0].click();", real_connect)

            time.sleep(1.5)
            screenshot(driver, "04_after_connect_click")

            # ── Step 6: Check for modal ──────────────────────────
            shadow_debug = driver.execute_script("""
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    if (!el.shadowRoot) continue;
                    const text = (el.shadowRoot.textContent || '').toLowerCase();
                    if (text.includes('add a note') || text.includes('invitation') || text.includes('send without') || text.includes('how do you know')) {
                        const btns = el.shadowRoot.querySelectorAll('button, a, textarea, [contenteditable]');
                        const info = [];
                        for (const b of btns) {
                            const r = b.getBoundingClientRect();
                            if (r.width === 0 && r.height === 0) continue;
                            const t = (b.innerText || '').trim().substring(0,40);
                            info.push({tag: b.tagName, text: t, w: Math.round(r.width), h: Math.round(r.height)});
                        }
                        return JSON.stringify({host: el.tagName + '.' + (el.className||'').substring(0,30), elements: info}, null, 2);
                    }
                }
                return 'NO MODAL FOUND';
            """)
            logger.info(f"  Shadow modal: {shadow_debug}")

            # Also check regular DOM for dialog/modal
            regular_modal = driver.execute_script("""
                const modals = document.querySelectorAll('div[role="dialog"], div.artdeco-modal');
                const results = [];
                for (const m of modals) {
                    const r = m.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {
                        results.push({
                            tag: m.tagName,
                            role: m.getAttribute('role'),
                            class: (m.className||'').substring(0,50),
                            text: (m.textContent||'').substring(0,200).trim()
                        });
                    }
                }
                return JSON.stringify(results, null, 2);
            """)
            logger.info(f"  Regular DOM modals: {regular_modal}")
            screenshot(driver, "05_modal_state")
        else:
            logger.info("  ⚠️ No real Connect button found at all")
            screenshot(driver, "04_no_connect_button")

        logger.info("=" * 60)
        logger.info("🏁 Debug complete — browser stays open 60s")
        logger.info("=" * 60)
        time.sleep(60)

    except Exception:
        logger.error(f"Fatal:\n{traceback.format_exc()}")
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    main()

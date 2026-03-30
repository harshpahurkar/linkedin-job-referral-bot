"""
Utility helpers — logging, browser setup, human-like delays.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
import random
import shutil
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

from config import Config


# ── Anti-detection: rotating User-Agent pool ──────────────────────────
# WINDOWS ONLY — using macOS UAs on a Windows machine creates an instant
# fingerprint contradiction (WebRTC/canvas/platform leak the real OS).
# Current Chrome versions (143–145) — updated March 2026.
_USER_AGENTS = [
    # Chrome 143 – Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    # Chrome 144 – Windows 10/11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    # Chrome 145 – Windows 10/11 (current stable)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
]

# ── Anti-detection: window size pool ──────────────────────────────────
# Common desktop resolutions so every session looks slightly different.
_WINDOW_SIZES = [
    (1920, 1080),
    (1366, 768),
    (1536, 864),
    (1440, 900),
    (1680, 1050),
    (1600, 900),
    (1280, 800),
]


def get_logger(name: str) -> logging.Logger:
    """Create a configured logger.

    Console: shows INFO+ (clean, readable output).
    File:    captures DEBUG+ (full diagnostic history for troubleshooting).
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        # Logger itself allows everything; handlers filter by level.
        logger.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s | %(name)-18s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Console — INFO+ only (clean output, no debug noise)
        ch = logging.StreamHandler()
        ch.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        # File — DEBUG+ (full history for post-mortem analysis)
        # Rotating: 5 MB per file, keep last 5 backups (~25 MB max)
        log_dir = Path(__file__).parent / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_dir / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


# ── Persistent bot profile directory ──────────────────────────────
# A SINGLE persistent profile is reused across runs.  This is critical:
# LinkedIn fingerprints fresh/empty profiles as bot behaviour.  A real
# user's Chrome profile accumulates cookies, localStorage, IndexedDB,
# service workers, and cache entries over time.  Nuking the profile
# every run was a major detection signal — every session looked like
# a brand-new browser that had never visited LinkedIn before.
_BOT_PROFILES_ROOT = Path(Config.CHROME_PROFILE_PATH).parent / "chrome-bot-profiles" \
    if Config.CHROME_PROFILE_PATH else Path.home() / ".linkedin-bot-profiles"
_PERSISTENT_PROFILE = _BOT_PROFILES_ROOT / "persistent"


def create_driver() -> webdriver.Chrome:
    """Spin up a Chrome driver with a fresh, disposable profile.

    A unique user-data-dir is created for this run. The path is stored
    on ``driver._bot_profile_dir`` so callers can nuke it via
    :func:`cleanup_driver`.
    """
    opts = Options()

    if Config.HEADLESS:
        opts.add_argument("--headless=new")

    # Persistent profile — reused across runs so LinkedIn sees an
    # established browser with cookies, cache, and history.  A fresh
    # profile every run was a major detection signal.
    _PERSISTENT_PROFILE.mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={_PERSISTENT_PROFILE}")

    # Anti-detection flags
    # Suppress Chrome's noisy internal logs (STUN, GCM, USB, TensorFlow)
    # These are harmless Chrome subsystem messages, not bot errors.
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-logging")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Extra anti-fingerprinting flags
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-features=IsolateOrigins,site-per-process")

    # Random window size each session
    win_w, win_h = random.choice(_WINDOW_SIZES)
    opts.add_argument(f"--window-size={win_w},{win_h}")

    # Random User-Agent each session
    chosen_ua = random.choice(_USER_AGENTS)
    opts.add_argument(f"user-agent={chosen_ua}")

    service = Service(ChromeDriverManager().install())
    # Redirect chromedriver's own logs away from console
    service.creation_flags = 0x08000000  # CREATE_NO_WINDOW (Windows)
    driver = webdriver.Chrome(service=service, options=opts)

    # ── selenium-stealth: patches fingerprint vectors ─────────────
    # Hides WebGL vendor, renderer, languages, platform, Chrome runtime,
    # and the hairline feature that bots leak.
    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel UHD Graphics 620",
        fix_hairline=True,
    )

    # Remove webdriver flag from navigator (belt-and-suspenders)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    # ── CDP detection mitigation ────────────────────────────────
    # Patch navigator.plugins to look like a real browser (non-empty)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": """
        // Override plugins to look real (empty plugins = headless/bot)
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });
        // Override languages (consistent with stealth config)
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
        // Prevent detection via permissions API timing attack
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
        // Chrome runtime should exist on real Chrome
        window.chrome = window.chrome || {};
        window.chrome.runtime = window.chrome.runtime || {};
    """})

    log = get_logger("utils")
    log.info(f"🖥️  Window: {win_w}×{win_h} | UA: ...Chrome/{chosen_ua.split('Chrome/')[1][:5]}")
    log.info(f"📂 Persistent profile: {_PERSISTENT_PROFILE.name}")

    return driver


def cleanup_driver(driver: webdriver.Chrome) -> None:
    """Quit the browser.  The persistent profile is kept for next run.

    Keeping the profile avoids the 'brand new browser' fingerprint
    that LinkedIn flags.  Cookies, cache, and localStorage persist.
    """
    log = get_logger("utils")
    try:
        driver.quit()
        log.info("🔒 Browser closed (persistent profile kept for next run).")
    except Exception:
        pass


def purge_old_profiles() -> None:
    """Delete any leftover disposable profile dirs from old bot versions."""
    log = get_logger("utils")
    if not _BOT_PROFILES_ROOT.exists():
        return
    cleaned = 0
    for child in _BOT_PROFILES_ROOT.iterdir():
        if child.is_dir() and child.name.startswith("run_"):
            shutil.rmtree(child, ignore_errors=True)
            cleaned += 1
    if cleaned:
        log.info(f"🧹 Purged {cleaned} leftover disposable profile dir(s).")


def human_delay(min_sec: float = 1.0, max_sec: float = 3.0):
    """Sleep for a random duration to mimic human behaviour.
    
    Occasionally adds an extra micro-pause to simulate
    hesitation / reading / distraction (15% chance).
    """
    base = random.uniform(min_sec, max_sec)
    # 15% chance of a longer 'human moment'
    if random.random() < 0.15:
        base += random.uniform(1.5, 4.0)
    time.sleep(base)


def long_delay():
    """Longer pause between major actions (message sends, page transitions).
    
    Uses a weighted random strategy so most pauses are moderate (30-75s)
    but occasionally there's a longer 'human' pause (1-2 min) to
    mimic someone checking their phone, reading a profile, etc.
    
    Also applies session fatigue multiplier when available.
    """
    # Try to apply fatigue multiplier from antidetect module
    try:
        from antidetect import get_session
        fatigue = get_session().fatigue_multiplier
    except Exception:
        fatigue = 1.0

    roll = random.random()
    if roll < 0.75:
        # 75% — normal pace
        base = random.uniform(Config.MESSAGE_DELAY_MIN, Config.MESSAGE_DELAY_MAX)
    elif roll < 0.92:
        # 17% — slightly longer pause (reading something)
        base = random.uniform(30, 60)
    else:
        # 8% — long pause (bathroom break, coffee, phone call)
        base = random.uniform(60, 120)

    time.sleep(base * fatigue)


def scroll_page(driver: webdriver.Chrome, scrolls: int = 3):
    """Scroll down a page with randomized distances (looks human)."""
    for _ in range(scrolls):
        # Random scroll distance so it never looks robotic
        distance = random.randint(350, 950)
        driver.execute_script(f"window.scrollBy(0, {distance});")
        human_delay(0.2, 0.5)


def human_move_and_click(driver: webdriver.Chrome, element) -> bool:
    """Move the mouse to an element with a slight random offset, then click.

    Uses Selenium ActionChains to generate real mouse-move events
    (pointermove / mousemove) before clicking — JS .click() fires
    zero mouse events, which is a bot fingerprint.
    Returns True if click succeeded.
    """
    try:
        actions = ActionChains(driver)
        # Move to element with a small random offset (±5px) to look natural
        x_off = random.randint(-5, 5)
        y_off = random.randint(-5, 5)
        actions.move_to_element_with_offset(element, x_off, y_off)
        # Small pause before clicking (like a real hand)
        actions.pause(random.uniform(0.1, 0.35))
        actions.click()
        actions.perform()
        return True
    except Exception:
        # Fallback to JS click if ActionChains fails
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                element,
            )
            return True
        except Exception:
            return False


def simulate_random_mouse_movement(driver: webdriver.Chrome):
    """Move the mouse to a random spot on the page to look alive.

    Called during "dwell time" to mimic someone reading a profile.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        actions = ActionChains(driver)
        x = random.randint(100, 800)
        y = random.randint(100, 500)
        actions.move_to_element_with_offset(body, x, y)
        actions.pause(random.uniform(0.3, 0.8))
        actions.perform()
    except Exception:
        pass  # non-critical, swallow silently


def clear_session_cookies(driver: webdriver.Chrome):
    """Clear cookies and browser storage for the current session.

    Used by login smoke tests to ensure each run starts from a clean
    auth state without touching the persistent profile on disk.
    """
    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    try:
        driver.execute_script(
            "window.localStorage && window.localStorage.clear();"
            "window.sessionStorage && window.sessionStorage.clear();"
        )
    except Exception:
        pass

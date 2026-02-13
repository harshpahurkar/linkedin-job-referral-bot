"""
Utility helpers — logging, browser setup, human-like delays.
"""

import logging
import random
import time
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
# Recent Chrome UA strings across platforms — one picked randomly per session.
_USER_AGENTS = [
    # Chrome 120 – Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 121 – Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 122 – Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 120 – macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 121 – macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
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
    """Create a configured logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
        fmt = logging.Formatter(
            "%(asctime)s | %(name)-18s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Console
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        # File
        log_dir = Path(__file__).parent / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / "bot.log", encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def create_driver() -> webdriver.Chrome:
    """Spin up a Chrome driver with comprehensive anti-detection."""
    opts = Options()

    if Config.HEADLESS:
        opts.add_argument("--headless=new")

    # Use a SEPARATE user-data-dir for the bot so it never conflicts
    # with the user's default Chrome profile (which blocks remote-debugging).
    bot_data_dir = str(Path(Config.CHROME_PROFILE_PATH).parent / "Chrome Bot Data") \
        if Config.CHROME_PROFILE_PATH else str(Path.home() / ".linkedin-bot-chrome")
    opts.add_argument(f"--user-data-dir={bot_data_dir}")

    # Anti-detection flags
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")

    # Random window size each session
    win_w, win_h = random.choice(_WINDOW_SIZES)
    opts.add_argument(f"--window-size={win_w},{win_h}")

    # Random User-Agent each session
    chosen_ua = random.choice(_USER_AGENTS)
    opts.add_argument(f"user-agent={chosen_ua}")

    service = Service(ChromeDriverManager().install())
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
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    # Remove webdriver flag from navigator (belt-and-suspenders)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    log = get_logger("utils")
    log.info(f"🖥️  Window: {win_w}×{win_h} | UA: ...Chrome/{chosen_ua.split('Chrome/')[1][:5]}")

    return driver


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
    
    Uses a weighted random strategy so most pauses are short (8-15s)
    but occasionally there's a longer 'human' pause (30-90s) to
    mimic someone checking their phone, reading a profile, etc.
    """
    roll = random.random()
    if roll < 0.70:
        # 70% — normal pace
        time.sleep(random.uniform(Config.MESSAGE_DELAY_MIN, Config.MESSAGE_DELAY_MAX))
    elif roll < 0.90:
        # 20% — slightly longer pause (looking at something)
        time.sleep(random.uniform(20, 40))
    else:
        # 10% — long pause (bathroom break, coffee, phone)
        time.sleep(random.uniform(45, 90))


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

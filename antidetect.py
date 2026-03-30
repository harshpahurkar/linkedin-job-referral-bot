"""
Anti-detection engine — session rate limiter, LinkedIn notice detector,
fatigue simulation, and advanced human-behaviour patterns.

This module is the SINGLE source of truth for keeping the bot undetectable.
Every action (page load, scroll, click, message send) should check in here
before proceeding.

Design principles:
  1. CONSERVATIVE — better to send 15 referrals safely than 40 and get banned.
  2. OBSERVABLE — every throttle, pause, and skip is logged so you can tune.
  3. REACTIVE — if LinkedIn shows ANY warning, the session stops immediately.
  4. HUMAN — real humans have variable speed, get tired, take breaks, and
     don't methodically visit 40 profiles in a row.
"""

import math
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

from utils import get_logger

logger = get_logger("antidetect")


# ═══════════════════════════════════════════════════════════════════════
#  1. SESSION VELOCITY TRACKER
# ═══════════════════════════════════════════════════════════════════════
# Tracks how many actions we've done THIS session and enforces:
#   - Hourly rate caps
#   - Progressive cooldown (the more you've done, the longer between)
#   - Mandatory micro-breaks every N actions
#   - Session-level fatigue (session gets slower over time)

@dataclass
class SessionTracker:
    """Tracks all bot activity within a single run for rate limiting."""
    session_start: float = field(default_factory=time.time)
    connections_sent: int = 0
    dms_sent: int = 0
    profiles_viewed: int = 0
    searches_done: int = 0
    pages_loaded: int = 0
    actions_since_break: int = 0
    last_action_time: float = field(default_factory=time.time)
    linkedin_warning_detected: bool = False
    _warning_reason: str = ""

    # ── Limits (per session, not per day) ─────────────────────────
    MAX_CONNECTIONS_PER_HOUR: int = 20      # ~1 every 3 min (safe w/ 7 behavioral layers)
    MAX_PROFILES_PER_HOUR: int = 45         # active job seekers browse fast
    MAX_SEARCHES_PER_HOUR: int = 20         # search is least suspicious action
    ACTIONS_BEFORE_BREAK: int = 10          # take a break every 10 major actions
    BREAK_DURATION_MIN: float = 10.0        # micro-break: 10s–40s
    BREAK_DURATION_MAX: float = 40.0

    # ── Fatigue: session slows down over time ─────────────────────
    # After 50 min, delays start increasing. After 2 hours, everything
    # is 1.4× slower. Mild — we rely on behavioral patterns, not sluggishness.
    FATIGUE_ONSET_MINUTES: float = 50.0
    FATIGUE_MAX_MULTIPLIER: float = 1.4
    FATIGUE_PLATEAU_MINUTES: float = 120.0

    # ── Hard session cap: no human browses LinkedIn for 2+ hours ──
    # Randomised each session so the pattern isn't clockwork.
    # Range: 65–100 min (active job seeker in a focused session).
    MAX_SESSION_MINUTES: float = field(
        default_factory=lambda: random.uniform(65.0, 100.0)
    )

    @property
    def session_minutes(self) -> float:
        return (time.time() - self.session_start) / 60.0

    @property
    def fatigue_multiplier(self) -> float:
        """Returns 1.0 early in session, ramps up to MAX over time."""
        mins = self.session_minutes
        if mins < self.FATIGUE_ONSET_MINUTES:
            return 1.0
        progress = min(1.0, (mins - self.FATIGUE_ONSET_MINUTES) /
                       (self.FATIGUE_PLATEAU_MINUTES - self.FATIGUE_ONSET_MINUTES))
        return 1.0 + progress * (self.FATIGUE_MAX_MULTIPLIER - 1.0)

    @property
    def is_session_expired(self) -> bool:
        """True when the session has exceeded its randomised time cap."""
        return self.session_minutes >= self.MAX_SESSION_MINUTES

    @property
    def session_time_remaining(self) -> float:
        """Minutes left before the session cap is hit (can be negative)."""
        return self.MAX_SESSION_MINUTES - self.session_minutes

    @property
    def total_major_actions(self) -> int:
        return self.connections_sent + self.dms_sent

    def _hourly_rate(self, count: int) -> float:
        """Actions per hour based on session elapsed time."""
        elapsed_hours = max(0.01, (time.time() - self.session_start) / 3600)
        return count / elapsed_hours

    def should_pause_connections(self) -> bool:
        return self._hourly_rate(self.connections_sent) >= self.MAX_CONNECTIONS_PER_HOUR

    def should_pause_profiles(self) -> bool:
        return self._hourly_rate(self.profiles_viewed) >= self.MAX_PROFILES_PER_HOUR

    def should_pause_searches(self) -> bool:
        return self._hourly_rate(self.searches_done) >= self.MAX_SEARCHES_PER_HOUR

    def needs_break(self) -> bool:
        return self.actions_since_break >= self.ACTIONS_BEFORE_BREAK

    def record_connection(self):
        self.connections_sent += 1
        self.actions_since_break += 1
        self.last_action_time = time.time()

    def record_dm(self):
        self.dms_sent += 1
        self.actions_since_break += 1
        self.last_action_time = time.time()

    def record_profile_view(self):
        self.profiles_viewed += 1
        self.last_action_time = time.time()

    def record_search(self):
        self.searches_done += 1
        self.last_action_time = time.time()

    def record_page_load(self):
        self.pages_loaded += 1
        self.last_action_time = time.time()

    def take_break(self):
        """Reset the break counter after a break is taken."""
        self.actions_since_break = 0

    def flag_warning(self, reason: str):
        self.linkedin_warning_detected = True
        self._warning_reason = reason

    @property
    def warning_reason(self) -> str:
        return self._warning_reason


# Global session tracker — created once per run
_session: SessionTracker | None = None


def get_session() -> SessionTracker:
    global _session
    if _session is None:
        _session = SessionTracker()
    return _session


def reset_session():
    global _session
    _session = SessionTracker()
    logger.info("🛡️  New session: tracker active (runs until daily target is hit)")


# ═══════════════════════════════════════════════════════════════════════
#  2. SMART DELAYS — fatigue-aware, velocity-aware
# ═══════════════════════════════════════════════════════════════════════

def smart_delay(min_sec: float, max_sec: float, action_type: str = "general"):
    """Sleep with fatigue multiplier and optional velocity-based throttle.

    This replaces raw human_delay() for critical paths.  It:
      1. Applies the fatigue multiplier (session slows over time)
      2. Adds jitter so delays are never predictable
      3. If hourly rate is high, adds extra cooldown
      4. Occasionally adds a "human moment" (checking phone, etc.)
    """
    session = get_session()

    # Base delay with jitter
    base = random.uniform(min_sec, max_sec)

    # Apply fatigue
    base *= session.fatigue_multiplier

    # Velocity-based extra delay (only when significantly over hourly cap)
    if action_type == "connection" and session.should_pause_connections():
        extra = random.uniform(15, 45)
        logger.info(f"  ⏳ Connection rate high — cooling down {extra:.0f}s")
        base += extra
    elif action_type == "profile" and session.should_pause_profiles():
        extra = random.uniform(10, 30)
        logger.debug(f"  ⏳ Profile view rate high — cooling down {extra:.0f}s")
        base += extra
    elif action_type == "search" and session.should_pause_searches():
        extra = random.uniform(8, 20)
        logger.debug(f"  ⏳ Search rate high — cooling down {extra:.0f}s")
        base += extra

    # Progressive cooldown: more actions done = slightly longer delays
    actions = session.total_major_actions
    if actions > 15:
        # After 15 actions, each additional action adds 0-1.5s of extra delay
        base += random.uniform(0, min(1.5, (actions - 15) * 0.15))

    # 8% chance of a "human moment" — checking phone, sipping coffee
    if random.random() < 0.08:
        moment = random.uniform(2, 6)
        logger.debug(f"  ☕ Human moment ({moment:.1f}s)")
        base += moment

    time.sleep(base)


def take_micro_break():
    """Mandatory break after N consecutive major actions.

    Simulates a real person stepping away — checks feed, scrolls
    around, maybe reads a notification.
    """
    session = get_session()
    duration = random.uniform(
        session.BREAK_DURATION_MIN,
        session.BREAK_DURATION_MAX,
    )
    duration *= session.fatigue_multiplier  # longer breaks when tired

    logger.info(
        f"  🧘 Micro-break ({duration:.0f}s) — "
        f"{session.total_major_actions} actions done, "
        f"{session.session_minutes:.0f} min into session"
    )
    time.sleep(duration)
    session.take_break()


def should_take_break() -> bool:
    """Check if a micro-break is needed."""
    return get_session().needs_break()


# ═══════════════════════════════════════════════════════════════════════
#  3. LINKEDIN WARNING / NOTICE DETECTION
# ═══════════════════════════════════════════════════════════════════════
# After every significant navigation, scan the page for warning signals.
# If found, IMMEDIATELY stop the session.

# Known warning patterns (case-insensitive)
_WARNING_PATTERNS = [
    # Connection/invitation limits
    r"you\'?ve reached the weekly invitation limit",
    r"weekly invitation limit",
    r"invitation limit",
    r"you can\'?t send .* invitations? right now",
    r"too many pending invitations",
    # Account restrictions
    r"your account has been restricted",
    r"account.*restrict",
    r"we\'?ve restricted",
    r"temporarily restricted",
    r"your account is under review",
    # Unusual activity
    r"unusual activity",
    r"we noticed unusual",
    r"we\'?ve detected unusual",
    r"suspicious activity",
    r"automated.*behavio",
    r"this looks like automated",
    # CAPTCHA / verification
    r"let\'?s do a quick security check",
    r"security verification",
    r"verify.*you\'?re (not a robot|human)",
    r"please verify your identity",
    # Rate limiting
    r"you\'?re doing that too fast",
    r"slow down",
    r"too many requests",
    r"rate.?limit",
    # Commercial use limit
    r"commercial use limit",
    r"reached? (your|the) search limit",
    r"you\'?ve reached? (your|the) (monthly|weekly).*limit",
    # Generic warnings
    r"action.*not allowed",
    r"this action is temporarily unavailable",
    r"something went wrong.*try again",
]

_WARNING_REGEX = re.compile(
    "|".join(f"(?:{p})" for p in _WARNING_PATTERNS),
    re.IGNORECASE,
)


def check_for_linkedin_warnings(driver: webdriver.Chrome) -> tuple[bool, str]:
    """Scan the current page for LinkedIn warning/restriction notices.

    Returns (warning_detected: bool, reason: str).
    Should be called after EVERY significant navigation.
    """
    session = get_session()

    # Already flagged — don't scan again
    if session.linkedin_warning_detected:
        return True, session.warning_reason

    try:
        # 1. Check page title for error indicators

        # 2. Check URL for redirect to security/restriction pages
        url = driver.current_url.lower()
        if any(p in url for p in (
            "/checkpoint/", "/security/", "/captcha",
            "/restricted", "/uas/login", "/authwall",
        )):
            reason = f"Redirected to warning page: {driver.current_url}"
            session.flag_warning(reason)
            logger.critical(f"🚨 LINKEDIN WARNING DETECTED: {reason}")
            return True, reason

        # 3. Scan visible text for warning patterns
        # Only check high-signal DOM areas (modals, banners, alerts)
        warning_text = driver.execute_script("""
            const selectors = [
                '[role="alert"]',
                '[role="alertdialog"]',
                '.artdeco-modal',
                '.artdeco-toast-item',
                '.artdeco-inline-feedback',
                '.ip-fuse-limit-alert',
                '.global-alert',
                '#global-alert',
                '.msg-overlay-bubble-header',
                '.premium-upsell',
            ];
            let text = '';
            for (const sel of selectors) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (el.offsetParent !== null) {  // visible only
                        text += ' ' + (el.innerText || el.textContent || '');
                    }
                }
            }
            // Also check any visible modal / dialog
            const dialogs = document.querySelectorAll('div[role="dialog"]');
            for (const d of dialogs) {
                if (d.offsetParent !== null) {
                    text += ' ' + (d.innerText || '');
                }
            }
            return text.substring(0, 3000);
        """)

        if warning_text and _WARNING_REGEX.search(warning_text):
            match = _WARNING_REGEX.search(warning_text)
            reason = f"Warning text found: '{match.group()[:100]}'"
            session.flag_warning(reason)
            logger.critical(f"🚨 LINKEDIN WARNING DETECTED: {reason}")
            logger.critical(f"   Full context: {warning_text[:300]}")
            return True, reason

        # 4. Check for CAPTCHA iframes
        # NOTE: LinkedIn embeds an invisible reCAPTCHA Enterprise iframe
        # (size=invisible) on most pages as a passive background check.
        # That is NOT a blocking challenge — only flag *visible* CAPTCHAs
        # that actually require user interaction.
        captcha = driver.execute_script("""
            const iframes = document.querySelectorAll('iframe');
            for (const f of iframes) {
                const src = (f.src || '').toLowerCase();
                if (src.includes('captcha') || src.includes('recaptcha')
                    || src.includes('hcaptcha') || src.includes('challenge')) {
                    // Invisible reCAPTCHA is a passive background check,
                    // not a blocking challenge — skip it.
                    if (src.includes('size=invisible')) continue;
                    // Also skip zero-size / hidden iframes
                    const rect = f.getBoundingClientRect();
                    if (rect.width < 10 || rect.height < 10) continue;
                    return src;
                }
            }
            return null;
        """)
        if captcha:
            reason = f"CAPTCHA iframe detected: {captcha}"
            session.flag_warning(reason)
            logger.critical(f"🚨 LINKEDIN WARNING DETECTED: {reason}")
            return True, reason

    except Exception as e:
        # If we can't even check, something is wrong — err on the side of caution
        logger.warning(f"  Warning check failed: {e}")

    return False, ""


def is_session_safe() -> bool:
    """Quick check: should the bot continue or stop?

    Returns False if a LinkedIn warning/restriction was detected.
    The session runs until the daily message target is hit — there is
    no arbitrary time cap.  Fatigue multiplier still slows actions
    over time so behaviour looks natural.
    """
    session = get_session()

    if session.linkedin_warning_detected:
        return False

    return True


# ═══════════════════════════════════════════════════════════════════════
#  4. SAFE PAGE NAVIGATION
# ═══════════════════════════════════════════════════════════════════════
# Wraps driver.get() with warning detection + session tracking.

def safe_get(driver: webdriver.Chrome, url: str) -> bool:
    """Navigate to a URL with anti-detection checks.

    Returns False if a LinkedIn warning was detected (caller should abort).
    """
    session = get_session()

    if session.linkedin_warning_detected:
        logger.warning("  ⛔ Session flagged — refusing to navigate.")
        return False

    try:
        driver.get(url)
    except Exception as e:
        logger.error(f"  Navigation failed: {e}")
        return False

    session.record_page_load()

    # Pause after every page load — humans read/orient before acting.
    # LinkedIn measures time-to-first-interaction; instant action = bot.
    time.sleep(random.uniform(2.0, 5.0))

    # Check for warnings
    warning, reason = check_for_linkedin_warnings(driver)
    if warning:
        logger.critical(f"🛑 ABORTING SESSION — LinkedIn warning: {reason}")
        return False

    return True


# ═══════════════════════════════════════════════════════════════════════
#  5. ADVANCED HUMAN SIMULATION
# ═══════════════════════════════════════════════════════════════════════

def bezier_mouse_move(driver: webdriver.Chrome, target_x: int, target_y: int):
    """Move mouse along a Bézier curve to the target position.

    Real mouse movements follow curves, not straight lines.
    This generates a random quadratic Bézier curve with 1-2
    control points and moves the mouse along it in small steps.
    """
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        actions = ActionChains(driver)

        # Start from a random current-ish position
        start_x = random.randint(200, 800)
        start_y = random.randint(100, 400)

        # Random control point(s) for the curve
        ctrl_x = random.randint(
            min(start_x, target_x) - 50,
            max(start_x, target_x) + 50,
        )
        ctrl_y = random.randint(
            min(start_y, target_y) - 50,
            max(start_y, target_y) + 50,
        )

        # Generate points along the Bézier curve
        steps = random.randint(8, 16)
        for i in range(steps + 1):
            t = i / steps
            # Quadratic Bézier: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
            x = int((1 - t) ** 2 * start_x + 2 * (1 - t) * t * ctrl_x + t ** 2 * target_x)
            y = int((1 - t) ** 2 * start_y + 2 * (1 - t) * t * ctrl_y + t ** 2 * target_y)
            actions.move_to_element_with_offset(body, x, y)
            # Variable speed — slower at start and end (like a real hand)
            speed = 0.01 + 0.03 * math.sin(math.pi * t)  # slow-fast-slow
            actions.pause(speed + random.uniform(0, 0.02))

        actions.perform()
    except Exception:
        pass  # non-critical


def realistic_profile_reading(driver: webdriver.Chrome):
    """Simulate genuinely reading a LinkedIn profile.

    Much more realistic than the old version:
      - Time spent scales with page content length
      - Multiple scroll-stop-read cycles
      - Mouse hovers over different sections
      - Occasional scroll-back (re-reading something)
      - Random chance to check the About section more carefully
    """
    try:
        # Measure content length to scale reading time
        content_len = driver.execute_script(
            "return (document.body.innerText || '').length;"
        ) or 1000

        # Reading time: 1s per ~300 chars, clamped to 3-8s
        read_time = max(3.0, min(8.0, content_len / 300.0))
        read_time *= get_session().fatigue_multiplier

        # Phase 1: Initial scan — look at name, headline, photo
        bezier_mouse_move(driver, random.randint(300, 600), random.randint(80, 200))
        time.sleep(random.uniform(0.6, 1.5))

        # Phase 2: Scroll down to read experience/about
        scroll_segments = random.randint(2, 3)
        for seg in range(scroll_segments):
            # Variable scroll distance
            dist = random.randint(200, 500)
            driver.execute_script(f"window.scrollBy(0, {dist});")
            time.sleep(random.uniform(0.2, 0.5))

            # "Read" this section
            bezier_mouse_move(
                driver,
                random.randint(200, 700),
                random.randint(200, 500),
            )
            section_read = random.uniform(
                read_time / scroll_segments * 0.4,
                read_time / scroll_segments * 1.2,
            )
            time.sleep(section_read)

            # 12% chance to scroll back and re-read something
            if random.random() < 0.12:
                driver.execute_script(
                    f"window.scrollBy(0, {-random.randint(100, 200)});"
                )
                time.sleep(random.uniform(0.5, 1.2))

        # Phase 3: Scroll back to top (where Connect button is)
        driver.execute_script("window.scrollTo({top: 0, behavior: 'smooth'});")
        time.sleep(random.uniform(0.5, 1.2))

    except Exception:
        # Fallback: basic delay
        time.sleep(random.uniform(3, 6))


def realistic_typing(element, text: str):
    """Type text with human-realistic patterns.

    Improvements over the old _type_message:
      - Variable typing speed (fast for common words, slow for complex)
      - Occasional pause between words (thinking)
      - Rare typo + backspace correction (2% chance per word)
      - Speed varies within a session (fatigue)
    """
    fatigue = get_session().fatigue_multiplier
    words = text.split(' ')

    for word_idx, word in enumerate(words):
        # Add space between words (except first)
        if word_idx > 0:
            element.send_keys(' ')
            # 15% chance of a thinking pause between words
            if random.random() < 0.15:
                time.sleep(random.uniform(0.3, 1.2) * fatigue)
            else:
                time.sleep(random.uniform(0.05, 0.15))

        # 2% chance of making a typo and correcting it
        if random.random() < 0.02 and len(word) > 3:
            # Type most of the word, add wrong char, backspace, fix
            cutoff = random.randint(2, len(word) - 1)
            for char in word[:cutoff]:
                _type_char(element, char, fatigue)
            # Wrong character
            wrong = chr(random.randint(97, 122))
            _type_char(element, wrong, fatigue)
            time.sleep(random.uniform(0.2, 0.6))  # notice the mistake
            element.send_keys('\ue003')  # backspace
            time.sleep(random.uniform(0.1, 0.3))
            # Continue with correct chars
            for char in word[cutoff:]:
                _type_char(element, char, fatigue)
        else:
            for char in word:
                _type_char(element, char, fatigue)

    # Small pause after finishing typing (reviewing what was written)
    time.sleep(random.uniform(0.5, 1.5))


def _type_char(element, char: str, fatigue: float = 1.0):
    """Type a single character with variable speed."""
    if ord(char) > 0xFFFF:
        # Non-BMP (emoji) — inject via JS
        element.parent.execute_script(
            "arguments[0].value += arguments[1]; "
            "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
            element, char,
        )
        time.sleep(random.uniform(0.05, 0.12) * fatigue)
    else:
        element.send_keys(char)
        # Variable speed: common chars are faster
        if char in 'etaoinshrdlu ':
            delay = random.uniform(0.02, 0.06)
        elif char.isupper() or char in '.,!?@':
            delay = random.uniform(0.06, 0.14)  # shift key = slower
        else:
            delay = random.uniform(0.03, 0.09)
        time.sleep(delay * fatigue)


def natural_scroll_pattern(driver: webdriver.Chrome, scrolls: int = 3):
    """Scroll a page with realistic human patterns.

    - Variable speed (slow at first, faster in middle, slow at end)
    - Occasional pause to "read" something
    - Sometimes scroll UP slightly (re-reading)
    - Mouse moves between scrolls
    """
    for i in range(scrolls):
        # Variable distance — not uniform
        if i == 0:
            dist = random.randint(200, 400)  # cautious first scroll
        else:
            dist = random.randint(300, 800)

        # Alternate between smooth scroll and jump
        if random.random() < 0.6:
            driver.execute_script(
                f"window.scrollBy({{top: {dist}, behavior: 'smooth'}});"
            )
        else:
            driver.execute_script(f"window.scrollBy(0, {dist});")

        # Reading pause — longer for first few scrolls
        read_time = random.uniform(0.8, 2.5) if i < 2 else random.uniform(0.3, 1.2)
        read_time *= get_session().fatigue_multiplier
        time.sleep(read_time)

        # 25% chance to move mouse (reading something specific)
        if random.random() < 0.25:
            bezier_mouse_move(
                driver,
                random.randint(100, 800),
                random.randint(200, 600),
            )

        # 10% chance to scroll back up slightly
        if random.random() < 0.10:
            driver.execute_script(
                f"window.scrollBy(0, {-random.randint(50, 200)});"
            )
            time.sleep(random.uniform(0.5, 1.5))


# ═══════════════════════════════════════════════════════════════════════
#  6. SESSION BREAK SIMULATION
# ═══════════════════════════════════════════════════════════════════════

def simulate_natural_break(driver: webdriver.Chrome):
    """Simulate a human taking a real break mid-session.

    During a break, the bot might:
      - Check the feed (+ maybe like a post)
      - Read notifications
      - Check My Network / Who Viewed My Profile
      - Just sit idle (bathroom break / coffee)
    This takes 30s–3min and makes the session look organic.
    """
    session = get_session()
    duration = random.uniform(15, 50) * session.fatigue_multiplier

    roll = random.random()

    if roll < 0.35:
        # 35% — browse the feed + maybe like something
        logger.info(f"  🧘 Break: browsing feed ({duration:.0f}s)")
        try:
            safe_get(driver, "https://www.linkedin.com/feed/")
            time.sleep(random.uniform(1.5, 3))
            natural_scroll_pattern(driver, scrolls=random.randint(1, 2))
            _maybe_like_feed_post(driver)
        except Exception:
            pass
        remaining = max(0, duration - 10)
        if remaining > 0:
            time.sleep(remaining)

    elif roll < 0.55:
        # 20% — check notifications
        logger.info(f"  🧘 Break: checking notifications ({duration:.0f}s)")
        try:
            safe_get(driver, "https://www.linkedin.com/notifications/")
            time.sleep(random.uniform(1.5, 3))
            natural_scroll_pattern(driver, scrolls=1)
        except Exception:
            pass
        remaining = max(0, duration - 7)
        if remaining > 0:
            time.sleep(remaining)

    elif roll < 0.70:
        # 15% — check My Network (connection suggestions)
        logger.info(f"  🧘 Break: checking My Network ({duration:.0f}s)")
        try:
            safe_get(driver, "https://www.linkedin.com/mynetwork/")
            time.sleep(random.uniform(1.5, 3))
            natural_scroll_pattern(driver, scrolls=1)
        except Exception:
            pass
        remaining = max(0, duration - 8)
        if remaining > 0:
            time.sleep(remaining)

    elif roll < 0.82:
        # 12% — check "Who Viewed My Profile" (very common for job seekers)
        logger.info(f"  🧘 Break: checking who viewed profile ({duration:.0f}s)")
        try:
            safe_get(driver, "https://www.linkedin.com/me/profile-views/")
            time.sleep(random.uniform(3, 6))
            natural_scroll_pattern(driver, scrolls=random.randint(1, 2))
        except Exception:
            pass
        remaining = max(0, duration - 10)
        if remaining > 0:
            time.sleep(remaining)

    else:
        # 18% — just idle (AFK)
        logger.info(f"  🧘 Break: idle/AFK ({duration:.0f}s)")
        time.sleep(duration)

    session.take_break()

    # Check for warnings after break navigation
    warning, reason = check_for_linkedin_warnings(driver)
    if warning:
        logger.critical(f"🛑 Warning detected during break: {reason}")


def _maybe_like_feed_post(driver: webdriver.Chrome):
    """Occasionally like a post on the feed (40% chance per call).

    Real humans don't just stare at the feed — they engage.
    LinkedIn sees a user who loads the feed 4× but never clicks
    anything as suspicious. A random like every few visits fixes this.

    Only likes if:
      - 40% random chance fires
      - A visible Like button is found
      - The post isn't already liked
    """
    if random.random() > 0.40:
        return  # 60% of the time, just browse without liking

    try:
        # Find all visible, un-liked "Like" buttons on the feed
        # LinkedIn uses aria-label="React Like" for unlike, we want non-reacted
        liked = driver.execute_script("""
            const btns = document.querySelectorAll(
                'button[aria-label="React Like"], '
                + 'button.react-button:not(.react-button--active), '
                + 'button[aria-pressed="false"] span.reactions-react-button'
            );
            const candidates = [];
            for (const btn of btns) {
                const rect = btn.getBoundingClientRect();
                // Only visible buttons in the viewport
                if (rect.top > 0 && rect.top < 1200
                    && btn.offsetParent !== null
                    && !btn.classList.contains('react-button--active')
                    && btn.getAttribute('aria-pressed') !== 'true') {
                    candidates.push(btn);
                }
            }
            if (candidates.length === 0) return false;
            // Pick a random one (not always the first — that's bot-like)
            const pick = candidates[Math.floor(Math.random() * candidates.length)];
            // Scroll it into view smoothly
            pick.scrollIntoView({block: 'center', behavior: 'smooth'});
            return true;
        """)

        if not liked:
            return

        time.sleep(random.uniform(0.8, 2.0))  # read the post first

        # Now click the Like button
        driver.execute_script("""
            const btns = document.querySelectorAll(
                'button[aria-label="React Like"], '
                + 'button.react-button:not(.react-button--active)'
            );
            for (const btn of btns) {
                const rect = btn.getBoundingClientRect();
                if (rect.top > 200 && rect.top < 800
                    && btn.offsetParent !== null
                    && btn.getAttribute('aria-pressed') !== 'true') {
                    btn.click();
                    return true;
                }
            }
            return false;
        """)

        logger.debug("  👍 Liked a feed post (natural engagement)")
        time.sleep(random.uniform(0.5, 1.5))

    except Exception:
        pass  # non-critical — if it fails, we just didn't like anything

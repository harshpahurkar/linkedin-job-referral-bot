"""Quick test: contract detection on Mukul's profile (known contract)."""
import time
from config import Config
from utils import get_logger, create_driver
from auth import login
from antidetect import reset_session, safe_get, get_session
from messenger import _is_contract_employee

logger = get_logger("test_mukul")
Config.HEADLESS = False
Config.LOG_LEVEL = "DEBUG"

driver = create_driver()
reset_session()
login(driver)
time.sleep(2)

# Known contract profile
safe_get(driver, "https://www.linkedin.com/in/mukuliskul/")
time.sleep(4)

print("\nRunning contract detection...")
result = _is_contract_employee(driver)
print(f"\n{'='*50}")
print(f"RESULT: {'CONTRACT (would skip)' if result else 'FULL-TIME (would message)'}")
print(f"{'='*50}")

# Also test a known full-time person from DB (if available)
from models import Database
db = Database()
row = db.conn.execute(
    "SELECT name, profile_url FROM contacts WHERE profile_url LIKE '%linkedin.com/in/%' LIMIT 1"
).fetchone()
if row:
    print(f"\nAlso testing: {row['name']} (from DB, expected full-time)")
    safe_get(driver, row['profile_url'])
    time.sleep(4)
    result2 = _is_contract_employee(driver)
    print(f"RESULT: {'CONTRACT (would skip)' if result2 else 'FULL-TIME (would message)'}")

print("\nDone. Closing in 10s...")
time.sleep(10)
driver.quit()

"""Quick script to inspect the scraped jobs database."""
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "data" / "jobs.db"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row
cur = conn.cursor()

rows = cur.execute(
    "SELECT job_id, title, company, location, url, LENGTH(description) as desc_len "
    "FROM jobs ORDER BY rowid"
).fetchall()

print(f"\n{'='*70}")
print(f"  Total jobs in DB: {len(rows)}")
print(f"{'='*70}\n")

for i, r in enumerate(rows, 1):
    print(f"{i:>2}. [{r['desc_len'] or 0:>4} chars]  {r['title']}")
    print(f"    Company:  {r['company']}")
    print(f"    Location: {r['location']}")
    print(f"    URL:      {r['url'][:80]}...")
    print()

# List all tables
tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"Tables: {[t['name'] for t in tables]}")

# Try to show run log from whatever table exists
for t in tables:
    name = t['name']
    if name != 'jobs':
        rows2 = cur.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
        print(f"\n{name} ({len(rows2)} rows):")
        for r in rows2:
            print(f"  {dict(r)}")

conn.close()

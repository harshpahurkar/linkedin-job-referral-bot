"""
Dry-run test for the email outreach feature.
Shows what would happen without sending any emails.
"""

from models import Database, Contact, Job
from email_outreach import discover_email, _pick_email_template, _scrape_website_email_pattern
from config import Config

db = Database()

# ── 1. Show contacts already in DB ──────────────────────────────────
print("=" * 80)
print("EXISTING MESSAGED CONTACTS IN DB")
print("=" * 80)
rows = db.conn.execute(
    "SELECT name, first_name, company, title, email, messaged, message_date "
    "FROM contacts WHERE messaged = 1 ORDER BY message_date DESC LIMIT 10"
).fetchall()
for r in rows:
    em = r["email"] if r["email"] else "(none)"
    print(f"  {r['name']:25s} | {r['company']:25s} | {r['title'] or '':30s} | {em}")
print(f"\nTotal messaged contacts: "
      f"{db.conn.execute('SELECT COUNT(*) FROM contacts WHERE messaged=1').fetchone()[0]}")

# ── 2. Test email discovery on real DB contacts ─────────────────────
print("\n" + "=" * 80)
print("EMAIL DISCOVERY TEST (on DB contacts)")
print("=" * 80)
all_contacts = db.conn.execute(
    "SELECT * FROM contacts WHERE messaged = 1 ORDER BY message_date DESC LIMIT 15"
).fetchall()

discovered = 0
for row in all_contacts:
    c = Contact(
        contact_id=row["contact_id"],
        name=row["name"],
        first_name=row["first_name"],
        profile_url=row["profile_url"],
        company=row["company"],
        title=row["title"] or "",
        location=row["location"] or "",
    )
    email = discover_email(c)
    status = "FOUND" if email else "MISS "
    if email:
        discovered += 1
    print(f"  [{status}] {c.name:25s} @ {c.company:25s} → {email or 'N/A'}")

print(f"\n  Discovery rate: {discovered}/{len(all_contacts)} "
      f"({100*discovered/max(len(all_contacts),1):.0f}%)")

# ── 3. Test on a mix of fake contacts (known + unknown companies) ───
print("\n" + "=" * 80)
print("EMAIL DISCOVERY TEST (synthetic contacts)")
print("=" * 80)
test_contacts = [
    Contact("t1", "Sarah Chen", "Sarah", "", "Shopify", "Software Engineer"),
    Contact("t2", "James Wilson", "James", "", "RBC", "Talent Acquisition Lead"),
    Contact("t3", "Maria Garcia", "Maria", "", "TD Bank", "Technical Recruiter"),
    Contact("t4", "Alex Park", "Alex", "", "Wealthsimple", "Backend Developer"),
    Contact("t5", "Priya Sharma", "Priya", "", "Lightspeed Commerce", "HR Manager"),
    Contact("t6", "David O'Brien", "David", "", "Clearco", "Engineering Manager"),
    Contact("t7", "Kim Nguyen", "Kim", "", "Random Startup Labs Inc.", "Recruiter"),
    Contact("t8", "Tom Brown", "Tom", "", "Acme Software", "DevOps Engineer"),
    Contact("t9", "Lisa Zhang", "Lisa", "", "Bench Accounting", "People Operations"),
    Contact("t10", "Mike Johnson", "Mike", "", "S.i. Systems", "Senior Developer"),
]
for c in test_contacts:
    email = discover_email(c)
    tag = "HR/Recruiter" if any(kw in (c.title or "").lower() for kw in
        ["recruiter", "talent", "hiring", "hr ", "human resource", "people operations"]) else "Technical"
    print(f"  [{tag:12s}] {c.name:20s} @ {c.company:25s} → {email or 'N/A'}")

# ── 4. Test email templates (dry run — shows what would be sent) ────
print("\n" + "=" * 80)
print("EMAIL TEMPLATE DRY RUN")
print("=" * 80)
test_job = Job(
    job_id="test_j1",
    title="Full-Stack Developer",
    company="Shopify",
    location="Toronto, ON",
    url="https://linkedin.com/jobs/view/123",
    description="Python React AWS Docker Kubernetes CI/CD agile scrum REST API",
)

# Test each contact type
for c in [test_contacts[0], test_contacts[1], test_contacts[4], test_contacts[7]]:
    email = discover_email(c)
    if not email:
        continue
    subject, body = _pick_email_template(c, test_job)
    is_recruiter = any(kw in (c.title or "").lower() for kw in
        ["recruiter", "talent", "hiring", "hr ", "human resource", "people operations"])
    print(f"\n  To: {c.name} ({c.title}) @ {c.company}")
    print(f"  Email: {email}")
    print(f"  Type: {'RECRUITER/HR' if is_recruiter else 'TECHNICAL'}")
    print(f"  Subject: {subject}")
    print(f"  Body preview ({len(body)} chars):")
    for line in body.split("\n")[:6]:
        print(f"    {line}")
    print(f"    ...")

# ── 5. Test website scraping on a few domains ───────────────────────
print("\n" + "=" * 80)
print("WEBSITE EMAIL PATTERN SCRAPING TEST")
print("=" * 80)
test_domains = ["clio.com", "vidyard.com", "fellow.app"]
for domain in test_domains:
    print(f"  Scraping {domain} ... ", end="", flush=True)
    fmt = _scrape_website_email_pattern(domain)
    print(f"format={'first.last (default)' if not fmt else fmt}")

print("\n" + "=" * 80)
print("DRY RUN COMPLETE — no emails were sent")
print("=" * 80)

db.close()

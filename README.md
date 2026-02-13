# 🤖 LinkedIn Job Referral Bot

Automated daily pipeline that scrapes LinkedIn jobs, finds employees at those companies, and sends personalized referral-request connection notes — all hands-free.

---

## 🚀 Quick Start (One Click)

**Double-click `run.bat`** — that's it. It cleans old data, launches Chrome, scrapes jobs, and starts sending referral requests.

---

## 📋 Full Setup (First Time Only)

### 1. Install Python dependencies

```powershell
cd linkedin-job-referral-bot
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy the example and fill in your details:

```powershell
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `LINKEDIN_EMAIL` | Your LinkedIn login email | — |
| `LINKEDIN_PASSWORD` | Your LinkedIn password | — |
| `JOB_KEYWORDS` | Comma-separated job titles to search | `Software Engineer` |
| `JOB_LOCATION` | Location filter | `Canada` |
| `EXPERIENCE_LEVEL` | `entry_level`, `associate`, `mid_senior`, etc. | `entry_level,associate` |
| `REMOTE_FILTER` | `remote`, `hybrid`, `on_site` | `remote,hybrid,on_site` |
| `DAILY_TARGET_MIN` | Min random daily connection target | `25` |
| `DAILY_TARGET_MAX` | Max random daily connection target | `40` |
| `MESSAGE_DELAY_MIN` | Min seconds between sends | `45` |
| `MESSAGE_DELAY_MAX` | Max seconds between sends | `120` |
| `MAX_CONNECTIONS_PER_WEEK` | Weekly safety cap | `180` |
| `CHROME_PROFILE_PATH` | Path to your Chrome user data | — |
| `YOUR_NAME` | Your name for message signature | `Harsh` |
| `YOUR_SCHOOL` | Your school (for alum template) | `Seneca College` |

### 3. Chrome profile (important)

The bot reuses your existing Chrome session so you don't have to log in or solve CAPTCHAs. Set this in `.env`:

```
CHROME_PROFILE_PATH=C:\Users\Harsh\AppData\Local\Google\Chrome\User Data
```

> ⚠️ **Close ALL Chrome windows before running the bot.** Chrome can only have one process using a profile at a time.

---

## 🎮 How to Run

### Option A: Double-click the launcher (recommended)

Just double-click **`run.bat`**. It will:
1. Kill any existing Chrome instances
2. Clean old log/db files for a fresh run
3. Launch the bot

### Option B: Run from terminal

```powershell
cd linkedin-job-referral-bot

# Clean old data
Remove-Item data\logs\bot.log, data\jobs.db -ErrorAction SilentlyContinue

# Launch (use Start-Process to avoid SIGINT crash in VS Code terminal)
Start-Process -FilePath "C:\Users\Harsh\Desktop\Projects\.venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory "C:\Users\Harsh\Desktop\Projects\linkedin-job-referral-bot"
```

### Option C: Dry run (scrape only, no messages)

```powershell
Start-Process -FilePath "C:\Users\Harsh\Desktop\Projects\.venv\Scripts\python.exe" -ArgumentList "main.py --dry-run" -WorkingDirectory "C:\Users\Harsh\Desktop\Projects\linkedin-job-referral-bot"
```

---

## 📊 Monitor Progress

While the bot is running, check the log:

```powershell
Get-Content data\logs\bot.log -Tail 30
```

Or watch it live:

```powershell
Get-Content data\logs\bot.log -Tail 10 -Wait
```

Check what's in the database:

```powershell
python -c "import sqlite3; conn = sqlite3.connect('data/jobs.db'); [print(f'{r[0]:50s} | {r[1]}') for r in conn.execute('SELECT title, company FROM jobs').fetchall()]; conn.close()"
```

---

## 🔄 What the Bot Does Each Run

1. **Weekly safety check** — stops if weekly limits already hit
2. **Picks random daily target** — between `DAILY_TARGET_MIN` and `DAILY_TARGET_MAX` (looks human)
3. **Clears old job data** — fresh scrape every run
4. **Launches Chrome** — reuses your logged-in session
5. **Scrapes jobs** — 5 keywords × 1 page each, jobs posted in last 24 hours
6. **For each company:**
   - Searches the company's LinkedIn /people/ page
   - Filters for **Canadian/North American** contacts only
   - Picks 4 engineers + 1 recruiter (max 5 per company)
   - Sends personalized connection note with referral ask
   - Waits 45–120 seconds between sends (anti-detection)
7. **Logs everything** — check `data/logs/bot.log`

---

## 🛡️ Safety Features

| Feature | Details |
|---|---|
| **Randomized daily volume** | Random target each run so LinkedIn can't pattern-match |
| **Weekly caps** | 180 connections/week, 1500 profile views/week |
| **Per-company limit** | Max 5 people per company (4 tech + 1 recruiter) |
| **Human-like delays** | 45–120s between sends, random pauses everywhere |
| **Geo filtering** | Only messages Canadian/North American contacts |
| **Company verification** | Verifies company name matches before browsing employees |
| **Contact filtering** | Blocks students, interns, freelancers, unemployed |
| **Duplicate detection** | SQLite DB prevents re-messaging the same person |
| **Template rotation** | 5 different message templates, matched to job type |

---

## 📝 Message Templates

5 templates auto-selected based on job type:

1. **Full-stack / frontend roles** — mentions React, Java, Python
2. **Backend / microservices** — mentions CI/CD, AWS, microservices
3. **School alum** — triggered when contact is a Seneca College grad
4. **Cloud / DevOps / infra** — mentions AWS, Azure, Docker, Kubernetes
5. **Catch-all** — generic SWE, used when no keyword matches

Edit templates in `config.py` → `REFERRAL_TEMPLATES`. Each must be under 300 characters (LinkedIn limit). Placeholders: `{first_name}`, `{job_title}`, `{company}`, `{your_name}`, `{school}`.

---

## 📂 Project Structure

```
linkedin-job-referral-bot/
├── run.bat             # 🟢 Double-click to launch
├── main.py             # Entry point & pipeline
├── config.py           # Loads .env, templates, validation
├── auth.py             # LinkedIn session check
├── scraper.py          # Job search & card parsing
├── messenger.py        # Employee search, geo filter, messaging
├── models.py           # SQLite DB & data classes
├── utils.py            # Browser setup, logging, delays
├── scheduler.py        # APScheduler daily cron
├── requirements.txt    # Python dependencies
├── .env                # Your config (not committed)
├── .env.example        # Config template
└── data/
    ├── jobs.db          # Auto-created SQLite DB
    └── logs/
        └── bot.log      # Run log
```

---

## ⚠️ Disclaimer

This tool automates browser interactions with LinkedIn. Use responsibly and at your own risk. LinkedIn may restrict or ban accounts that violate their Terms of Service. Start with `--dry-run` to verify behavior before enabling outreach.

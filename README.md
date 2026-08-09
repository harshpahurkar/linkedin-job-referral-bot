
# 🤖 LinkedIn Job Referral Bot

Automated pipeline that scrapes LinkedIn job listings, finds employees at target companies, and sends short, personalized referral-request connection notes. This README reflects the current codebase (multi-page scraping, relevance scoring, senior-role filtering, DM handling for already-connected contacts, and safer run patterns).


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

## 🧪 Testing

Run the automated test suite before changing selectors, filters, scoring, or safety limits:

```powershell
python -m pytest
```

Or double-click **`run_tests.bat`** on Windows. The tests cover the pure referral-bot logic: geo filtering, job ranking, company matching, contact filtering, message length, database dedupe, weekly activity counting, and profile-view safety caps.

---

## 🔄 What the Bot Does Each Run

1. **Weekly safety check** — stops if weekly limits already hit
2. **Picks random daily target** — between `DAILY_TARGET_MIN` and `DAILY_TARGET_MAX` (looks human)
3. **Clears old job data** — fresh scrape every run
4. **Launches Chrome** — reuses your logged-in session
5. **Scrapes jobs** — runs multi-page job searches for each configured keyword (default: 3 pages per keyword). Each job is scored by a relevance heuristic and the pipeline excludes senior-level roles by default.
6. **For each company:**
    - Try the company's `/people/` tab first for a natural-looking employee list; fall back to a People search when needed.
    - People search uses network filters (1st/2nd/3rd degree) so already-connected contacts appear and can be DM'd.
    - Extracts and validates only real `/in/` profile URLs, filters geo (Canadian / North American preferred) and relevant titles.
    - Builds a larger candidate pool and then sends up to `MAX_MESSAGES_PER_COMPANY` successful outreach attempts — connections or DMs. The loop caps successful sends (not raw results) so the bot keeps iterating until the per-company quota of successful messages is reached or exhausted.
    - If a contact is already connected and has not been messaged before, the bot will open the messaging overlay and send a DM (instead of skipping them).
    - Default contact mix: up to 4 technical contacts + 1 recruiter (configurable in code).
    - Waits between sends (human-like delays and longer waits after important actions).
7. **Logs everything** — check `data/logs/bot.log` for per-job and per-company activity and run summary.

---

## 🛡️ Safety Features

| Feature | Details |
|---|---|
| **Randomized daily volume** | Random target each run so LinkedIn can't pattern-match |
| **Weekly caps** | 180 connections/week, 1500 profile views/week |
| **Per-company limit** | Caps successful outreach per company (`MAX_MESSAGES_PER_COMPANY`) — the bot builds a larger candidate pool and only counts successful sends toward the per-company cap |
| **Human-like delays & behaviour** | 45–120s between sends, randomized pauses, mouse movement and profile-dwell simulation, increased scrolls on people pages |
| **Geo filtering** | Prefers Canadian/North American contacts (configurable) |
| **Company verification & people-page first** | Verifies company match before browsing `/people/` and falls back to People search when needed |
| **Search network filter** | People search includes 1st/2nd/3rd degree results so already-connected contacts are included (and messaged via DM if unmessaged)
| **Contact filtering** | Blocks students, interns, freelancers, unemployed; hard-filters senior/lead/manager roles by default |
| **Anti-detection** | Randomly skips a small percentage (~12%) of companies per run to break exhaustive patterns |
| **Duplicate detection** | SQLite DB prevents re-messaging the same person; already-messaged people are skipped |
| **Template rotation** | 5 different message templates, matched to job type |
| **Relevance scoring** | Job cards are scored and top-N selection is used so outreach prioritizes higher-quality openings |

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

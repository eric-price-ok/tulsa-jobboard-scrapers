# TulsaJobSpot Scraper Integration Guide

This document gives a new Claude Code session (or a developer) full context on the TulsaJobSpot project and how the scrapers in this repository connect to it. Read this before making any changes to scraper scripts or proposing changes to the web application.

---

## 1. Project Overview

**TulsaJobSpot** is a FastAPI web application that aggregates Tulsa-area job listings. It lives at:

- **Web app repo**: `C:\Users\ericp.ENDUROPLS\source\repos\tulsa-job-spot`
- **Scrapers repo**: `C:\Users\ericp.ENDUROPLS\source\repos\tjs-scrapers` (this repo)
- **Production**: deployed on a Linux server via Docker Compose
- **Live URL**: the public-facing job board

The scrapers collect job listings from employer job boards (ADP, Workday, Paycom, etc.) and write directly to the same PostgreSQL database the web app reads from.

---

## 2. Tech Stack

### Web Application
- **Framework**: FastAPI (Python), async, SQLAlchemy 2.0 ORM
- **Templates**: Jinja2, HTMX 2.0.4, Alpine.js
- **Database**: PostgreSQL (`tulsajobspot` DB, `tulsajobspot` user)
- **Auth**: OAuth only (Google), session cookie with `itsdangerous.TimestampSigner`
- **Deployment**: Docker Compose with `docker-compose.yml` + `docker-compose.prod.yml` overlay

### Scrapers
- **Language**: Python 3.x (sync, psycopg3)
- **Browser automation**: Selenium + ChromeDriver (headless Chrome)
- **HTTP**: `requests` library for API calls
- **HTML parsing**: BeautifulSoup4
- **DB driver**: `psycopg` (v3 sync) with `dict_row` factory

---

## 3. Database Connection

### Credentials
Never hardcoded. Always read from environment variables:

| Variable          | Default         | Notes                          |
|-------------------|-----------------|--------------------------------|
| `POSTGRES_HOST`   | `localhost`     | Server hostname or IP          |
| `POSTGRES_PORT`   | `5432`          | Standard PostgreSQL port       |
| `POSTGRES_DB`     | `tulsajobspot`  | Database name                  |
| `POSTGRES_USER`   | `tulsajobspot`  | PostgreSQL role                |
| `POSTGRES_PASSWORD` | *(none — required)* | Must be set; no default |

**Setting credentials before running a scraper on Windows:**
```cmd
set POSTGRES_HOST=<server-ip>
set POSTGRES_PORT=5432
set POSTGRES_DB=tulsajobspot
set POSTGRES_USER=tulsajobspot
set POSTGRES_PASSWORD=<password-from-.env>
python adp/ok-cancer-spec-adp-api-selenium.py
```

**On the Linux server (in the scraper directory):**
```bash
export POSTGRES_HOST=localhost
export POSTGRES_PORT=5432
export POSTGRES_DB=tulsajobspot
export POSTGRES_USER=tulsajobspot
export POSTGRES_PASSWORD=<password-from-.env>
python3 adp/ok-cancer-spec-adp-api-selenium.py
```

### Shared utility: `db_connection.py`
```python
from db_connection import get_database_connection, close_connection, test_connection

conn = get_database_connection()   # returns psycopg conn with autocommit=True, dict_row
# ... do work ...
close_connection(conn)
```

`test_connection()` returns True/False — use it at script startup to fail fast.

### Running scrapers locally vs. on the server
The PostgreSQL port (5432) is blocked by UFW on the production server and **must not be opened publicly**. This means:
- You **cannot** connect directly from a Windows workstation to the production DB.
- Scrapers must run **on the server** (SSH in, then run).
- Use `scp` or `git push` to get scripts to the server.

If you need to test DB logic locally, stand up a local PostgreSQL instance with the same schema (`docs/create-tulsajobspot-db.sql` in the web app repo) and point `POSTGRES_HOST=localhost` at it.

---

## 4. Database Schema

### Naming conventions (critical — old scrapers used wrong names)
All table names are **lowercase with no underscores** unless specified below.

| Old name (wrong)   | Correct name       |
|--------------------|--------------------|
| `JobListings`      | `joblistings`      |
| `JobStatus`        | `jobstatus`        |
| `Company`          | `company`          |
| `CompanySite`      | `companysite`      |
| `ScrapingLog`      | `scrapinglog`      |
| `Functions`        | `functions`        |
| `JobType`          | `jobtype`          |

### Key tables for scrapers

#### `company`
```sql
id                      serial PK
slug                    varchar(255) UNIQUE NOT NULL   -- URL slug, required
common_name             varchar(255) NOT NULL
legal_name              varchar(255)
website                 varchar(500)
jobboard                varchar(500)
company_type            int4 NOT NULL  → company_type.id
approved                bool DEFAULT false             -- must be set to true manually for new companies
is_scraped              bool DEFAULT false             -- set TRUE when created by scraper
defunct                 bool DEFAULT false             -- if true, hidden from public
last_full_scrape_completed timestamp                  -- updated after each complete scrape run
created_at, updated_at  timestamp
```

#### `joblistings`
```sql
id                      serial PK
company_id              int4 NOT NULL → company.id
company_site_id         int4 → companysite.id
posted_by               int4 → users.id  (NULL for scraper-sourced)
job_title               varchar(500) NOT NULL
job_description         text
posting_id              varchar(255)   -- employer's internal ID
posting_url             varchar(1000)  -- full URL to job detail page
application_method      varchar(20) DEFAULT 'external_url'  -- 'external_url'|'email'|'in_platform'
date_posted             date
date_closed             date
approved                bool DEFAULT false  -- scraped jobs start unapproved; admin reviews queue
job_status_id           int4 NOT NULL → jobstatus.id
function                int4 → functions.id
job_type_id             int4 → jobtype.id
city_id                 int4 → cities.id
minimum_salary          numeric(10,2)
maximum_salary          numeric(10,2)
pay_frequency           varchar(50)    -- 'hourly'|'daily'|'weekly'|'biweekly'|'monthly'|'annually'
source_job_board        varchar(100)   -- human-readable name e.g. 'Oklahoma Cancer Specialists ADP'
external_job_id         varchar(255)   -- employer's job ID from their system
scraping_hash           varchar(64)    -- MD5 for duplicate detection
last_scraped            timestamp DEFAULT now()  -- updated every scrape cycle
created_at, updated_at  timestamp
```

**Critical rules for scrapers writing to `joblistings`:**
- Do NOT set `approved=True`. Leave it out — the DB default is `false`. A site admin reviews the pending queue.
- Always look up `job_status_id` by name: `SELECT id FROM jobstatus WHERE name = 'active'` — never hardcode an integer.
- Update both `updated_at` AND `last_scraped` when a previously-seen job is confirmed to still exist.

#### `companysite`
```sql
id              serial PK
company_id      int4 NOT NULL → company.id
site_type       int4 → companysitetype.id
address1        varchar(255)
address2        varchar(255)
country_id      int4 → country.id
state_id        int4 → state.id
city_id         int4 → cities.id   -- FK only; there is NO freetext city column
postcode        varchar(10)
phone           varchar(50)
shortname       varchar           -- used by scrapers to identify locations
is_headquarters bool DEFAULT false
is_active       bool DEFAULT true
```

> **No `city` text column.** Earlier scraper code tried to INSERT a `city` column — this was removed in the new schema. Use `city_id` (FK to `cities`) only.

#### `jobstatus` — seed values
| name      | meaning                              |
|-----------|--------------------------------------|
| `active`  | Live, accepting applications         |
| `closed`  | No longer accepting applications     |
| `expired` | Past close date or link broken       |
| `draft`   | Not yet published                    |

> **Status names are lowercase.** Old code used `'Active'` — this is wrong and will fail the lookup.

#### `scrapinglog`
```sql
id              serial PK
job_board       varchar(100) NOT NULL   -- human label e.g. 'Oklahoma Cancer Specialists ADP'
company_id      int4 → company.id
jobs_found      int4 DEFAULT 0
jobs_added      int4 DEFAULT 0
jobs_updated    int4 DEFAULT 0
jobs_skipped    int4 DEFAULT 0
errors          text
status          varchar(20)   -- 'running'|'completed'|'failed'|'cancelled'
started_at      timestamp DEFAULT now()
completed_at    timestamp
duration_seconds int4        -- auto-calculated by trigger
```

A DB trigger (`update_scraping_duration`) automatically sets `duration_seconds` when `completed_at` is set.

#### `scraper_sources` — future scheduling table
```sql
id              serial PK
name            varchar(100)
scraper_class   varchar(100)   -- e.g. module path or filename
url             varchar(1000)  -- company's job board URL
company_id      int4 → company.id
config          jsonb          -- arbitrary config dict for the scraper
cron_schedule   varchar(50) DEFAULT '0 3 * * *'
is_active       bool DEFAULT true
selenium_required bool DEFAULT false
last_run_at     timestamp
last_status     varchar(20)
```

This table exists in the schema and is intended to power an admin scheduling UI. It is not yet wired up to any execution system. See Section 7.

### Reference data (lookup tables with seed values)

#### `functions`
Accounting, Administrative, Customer Support, Education, Engineering, Executive,
Healthcare, Hospitality, Human Resources, Information Technology, Law Enforcement,
Legal, Manufacturing, Operations, Other, Product, Purchasing, Quality, Sales,
Science, Security, Skilled Trades

> **This is the full, exact list — nothing else exists.** When writing a
> `_FUNCTION_KEYWORDS`-style dict for a new scraper, every category key MUST
> be one of these 22 names, verbatim. Many existing scrapers predate this
> list being documented and use invented names that don't exist here (e.g.
> `Administration` instead of `Administrative`, `Finance` instead of
> `Accounting`, `Customer Service` instead of `Customer Support`,
> `Skilled Labor` instead of `Skilled Trades`, `Healthcare Provider` instead
> of `Healthcare`, `Marketing`/`Project Management`/`Transportation/Logistics`
> with no equivalent at all, `Engineering, Mechanical` etc. instead of plain
> `Engineering`). A lookup failure here does NOT raise an error or log a
> warning distinct from a genuine no-keyword-match — it silently falls
> through to `Other`, so this class of bug is invisible without an explicit
> audit against this list.

#### `company_type`
Private Company, Public Company, Non-Profit, Government / Public Sector, Startup

#### `companysitetype`
Headquarters, Branch Office, Remote Office, Warehouse, Retail Location

#### `jobtype`
Full-time, Part-time, Contract, Contract-to-hire, Internship, Temporary

#### `officelocations`
On-site, Remote, Hybrid

#### `cities` (served = appears in filters)
Tulsa, Broken Arrow, Owasso, Bixby, Jenks, Sand Springs, Sapulpa, Claremore, Catoosa, Collinsville, Glenpool

#### `country` / `state` IDs used in scraper inserts
- US country: look up `SELECT id FROM country WHERE iso_code_2 = 'US'`
- Oklahoma state: look up `SELECT id FROM state WHERE name = 'Oklahoma'`
- Do not assume these are ID 1 — look them up by name.

---

## 5. Shared Utility Files

These files live in the root of this repo and are imported by newer scrapers. **Older scrapers pre-date them and contain their own inline versions of this logic** — check each scraper individually.

### `db_connection.py`
- `get_database_connection()` — returns psycopg conn, autocommit, dict_row
- `test_connection()` — quick connection check, returns bool
- `close_connection(conn)` — safe close
- `execute_with_retry(conn, func, ...)` — retry on transient connection errors

### `posting_operations.py`
- `store_job_listing(cursor, job_data, company_id)` — dynamic INSERT, does NOT set `approved`
- `check_existing_job_by_url(cursor, url)` — returns job_id if found, updates timestamps
- `check_existing_job_by_hash(cursor, hash)` — duplicate detection by hash
- `update_job_listing(cursor, job_id, job_data)` — dynamic UPDATE
- `load_active_jobs_cache(cursor, company_id)` — returns `{url: job_id}` dict for batch processing
- `check_job_in_cache(url, cache)` — dict lookup (no DB hit)
- `update_job_verified_timestamp(cursor, job_id)` — updates `updated_at` + `last_scraped`
- `mark_stale_jobs_closed(cursor, company_id)` — closes jobs not touched in current scrape cycle

### `company_operations.py`
- `get_or_create_company(cursor, company_data)` — looks up by `common_name`; creates if missing (generates slug, sets `is_scraped=True`); `company_data` dict keys: `name`, `website`, `jobboard`, optionally `company_type_name`
- `get_or_create_company_site(cursor, company_id, location_name, city_id)` — looks up by `shortname`; creates if missing (no freetext city column)
- `get_company_by_id / get_company_by_name / get_company_config_by_name` — read-only lookups
- `update_company_website / update_company_jobboard` — targeted updates

### `date_utilities.py`
Date parsing helpers. Scrapers call this for normalizing employer date formats.

### `utility_methods.py`
Miscellaneous shared helpers (HTML cleaning, salary parsing, etc.).

---

## 6. Scraper Inventory

Scrapers are organized by the job board platform they target:

| Folder       | Platform           | # of scrapers  |
|--------------|--------------------|----------------|
| `adp/`       | ADP Workforce Now  | 5              |
| `workday/`   | Workday            | 10             |
| `paycom/`    | Paycom             | 2              |
| `paylocity/` | Paylocity          | 1              |
| `applitrack/`| Applitrack/Frontline| 3             |
| `ultipro/`   | UltiPro/UKG       | 10             |
| `custom/`    | One-off scrapers   | ~10            |

### Scraper generations

**Generation 1 (older, monolithic):** The scraper contains its own `DatabaseManager` class with all SQL inline. These do NOT import from `db_connection.py`, `posting_operations.py`, or `company_operations.py`. Each must be updated individually.  
Example: `adp/ok-cancer-spec-adp-api-selenium.py`

**Generation 2 (newer, modular):** Imports from the shared utility files. When shared utilities are fixed, these benefit automatically.

Check each script for `from db_connection import` — presence means it's modular.

### Status of `adp/ok-cancer-spec-adp-api-selenium.py` (first proven scraper)
This is a Generation 1 monolithic scraper for **Oklahoma Cancer Specialists** (company_id: 911 hardcoded). It has been updated to work with the new schema:
- All table names corrected to lowercase
- `approved` removed from INSERT (DB default is false)
- Job status lookups use name subqueries instead of hardcoded integers
- Connection string reads from env vars (defaults to `tulsajobspot` db/user)
- `last_scraped` updated alongside `updated_at` when a job is verified

The scraper flow:
1. Hit ADP API to get all job requisitions for the company
2. Filter to Tulsa locations only
3. For each new job: scrape detail page with Selenium, extract description
4. Check `joblistings` for existing URL — if found, update timestamps and skip
5. Insert new jobs (unapproved, status=active)
6. After all jobs processed: mark stale jobs closed, update `company.last_full_scrape_completed`
7. Write summary to `scrapinglog`

---

## 7. Open Questions and Recommended Approaches

### A. Testing Scrapers

**The problem**: Scrapers write to the production database. If something is wrong, bad data lands in the approval queue. We can't connect to production DB from a dev machine (UFW blocks port 5432).

**Recommended approach — run on the server with `approved=False` as a safety net:**

Scraped jobs already land with `approved=False` (DB default), so they are invisible to public users until manually approved by an admin. This means running a scraper "for real" on the server is relatively safe — bad/duplicate data ends up in the pending queue rather than on the public site.

The practical proof-out workflow:
1. SSH to the production server
2. `cd` to the scrapers directory
3. Set env vars (copy from the `.env` file in the web app directory)
4. Run the scraper: `python3 adp/ok-cancer-spec-adp-api-selenium.py`
5. Check `scrapinglog` for the summary: `psql -U tulsajobspot -d tulsajobspot -c "SELECT * FROM scrapinglog ORDER BY started_at DESC LIMIT 5;"`
6. Check pending jobs: `psql -U tulsajobspot -d tulsajobspot -c "SELECT id, job_title, created_at FROM joblistings WHERE approved=false ORDER BY created_at DESC LIMIT 20;"`
7. If results look good, approve a few manually in the admin UI; delete bad ones

**Optional: add a `--dry-run` flag to scrapers.**  
A `dry_run=True` parameter passed to `store_job_listing` (or checked before any INSERT) would log what would be written without actually committing. This is worth adding to the Gen 1 scrapers once the basic proof-out is done.

**Local testing with a local DB** (more effort, more isolation):
```bash
# On Windows, install PostgreSQL locally
# Apply the schema:
psql -U postgres -f tulsa-job-spot/docs/create-tulsajobspot-db.sql
# Apply seed data:
cd tulsa-job-spot && python -m app.scripts.seed
# Run the scraper against local DB:
set POSTGRES_HOST=localhost
set POSTGRES_DB=tulsajobspot
set POSTGRES_USER=tulsajobspot
set POSTGRES_PASSWORD=localpassword
python adp/ok-cancer-spec-adp-api-selenium.py
```

### B. Scheduling System (Admin UI in the Web App)

**The problem**: Scrapers currently run manually. The database already has a `scraper_sources` table designed to hold scheduling configuration, but nothing executes based on it yet.

**What novice users actually need:**
- See a list of scrapers and their last-run status
- Enable/disable a scraper
- Set a schedule (cron expression, or simpler "run daily at 3am" style)
- See the log of recent runs (from `scrapinglog`)
- Be warned if a scraper failed

**Recommended architecture (two phases):**

**Phase 1 — Execution without UI (minimal work, working system):**

Add a `runner.py` script in the scrapers repo root that:
1. Connects to the DB
2. Queries `scraper_sources WHERE is_active=true`
3. For each source, checks whether it's due to run (compares `last_run_at` against `cron_schedule` using `croniter` library)
4. Invokes the appropriate scraper as a subprocess
5. Updates `last_run_at` and `last_status` after completion

Then add a single cron entry on the server:
```
*/15 * * * * /path/to/venv/bin/python3 /path/to/tjs-scrapers/runner.py >> /var/log/scraper-runner.log 2>&1
```

This gives you scheduling without touching the web app. Adding a new scraper = insert a row in `scraper_sources`.

**Phase 2 — Admin UI in the web app (proper UX):**

Add a page in the FastAPI admin section (`app/routers/admin/`) with:
- Table showing all `scraper_sources` rows with last-run status from `scrapinglog`
- Enable/disable toggle (sets `scraper_sources.is_active`)
- Schedule editor (validates cron expression)
- Link to the last N rows of `scrapinglog` for that source
- A "Run Now" button that triggers the runner via a subprocess or queue

For "Run Now" you have two sub-options:
- **Simple**: HTTP POST to an internal endpoint that `subprocess.Popen`s the scraper in the background (acceptable for infrequent manual triggers)
- **Better**: A Redis task queue (Celery or rq) — the web app already has a Redis service in production (`docker-compose.prod.yml` likely defines a worker service). This is the right choice if scrapers run frequently or are long-running.

**What to build first**: Start with Phase 1 (`runner.py` + server cron). That gets you a working scheduled system in an hour. Phase 2 (admin UI) is a multi-day UI project — plan it separately once Phase 1 is proven.

---

## 8. Web App Admin Areas Relevant to Scrapers

The FastAPI app has an admin section at `/admin` (requires `is_admin=True` on the user). Relevant admin capabilities:

- **Approve a job**: Sets `joblistings.approved=True`; scraped jobs land here first
- **Approve a company**: Sets `company.approved=True`; auto-created companies (is_scraped=True) are hidden until approved
- **Disable a company**: Sets `company.defunct=True`; hides from public, returns 404 on profile page
- **View scrapinglog**: Not yet in the UI — check the DB directly for now

The companies browse page only shows `approved=True AND defunct=False` companies. The jobs browse page only shows `approved=True AND job_status_id = (SELECT id FROM jobstatus WHERE name='active')` listings.

---

## 9. Working With a Specific Scraper

When picking up a scraper that hasn't been converted yet:

1. **Check generation**: does it `from db_connection import`? If not, it's Gen 1.
2. **Find all SQL strings**: grep for `FROM `, `INSERT INTO`, `UPDATE `. Fix any capitalized table names.
3. **Find `approved=True` in INSERTs**: remove it from both company and job listing inserts.
4. **Find hardcoded status IDs** (`job_status_id = 1`, `= 6`, etc.): replace with named subqueries.
5. **Find the connection string**: make sure it reads from env vars and defaults to `tulsajobspot`.
6. **Check companysite INSERTs**: remove any `city` column — it doesn't exist in the new schema.
7. **Test on the server** using the workflow in Section 7A.

---

## 10. File Locations Quick Reference

```
tjs-scrapers/                       # This repo (scrapers)
  db_connection.py                  # Shared DB connection utility
  posting_operations.py             # Shared job listing CRUD
  company_operations.py             # Shared company CRUD
  date_utilities.py                 # Date parsing helpers
  utility_methods.py                # Misc helpers
  docs/
    scraper-integration-guide.md    # This document
  adp/
    ok-cancer-spec-adp-api-selenium.py  # Gen 1 — UPDATED, ready to test
    template-adp-api-selenium.py        # Template for new ADP scrapers
  workday/ paycom/ paylocity/ applitrack/ ultipro/ custom/
    [individual scraper scripts]

tulsa-job-spot/                     # Web app repo (separate)
  app/
    models/
      company.py    # Company, CompanySite, CompanySocial ORM models
      job.py        # JobListing ORM model
      scraping.py   # ScraperSource, ScrapingLog ORM models
      reference.py  # All lookup table models (JobStatus, JobType, etc.)
    routers/
      companies.py  # Company browse + profile + admin disable/enable
      jobs.py       # Job browse
      admin/        # Admin-only routes
  docs/
    create-tulsajobspot-db.sql  # Full schema DDL (authoritative)
  app/scripts/seed.py           # Reference data seed values
```

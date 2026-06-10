# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running Scrapers

Scrapers must run **on the Linux production server** — the PostgreSQL port (5432) is UFW-blocked from outside. Set environment variables before running:

```bash
export POSTGRES_HOST=<server-ip>
export POSTGRES_PORT=5432
export POSTGRES_DB=tulsajobspot
export POSTGRES_USER=tulsajobspot
export POSTGRES_PASSWORD=<password>
python3 adp/ok-cancer-spec-adp-api-selenium.py
```

After a run, verify results:
```bash
psql -U tulsajobspot -d tulsajobspot -c "SELECT * FROM scrapinglog ORDER BY started_at DESC LIMIT 5;"
psql -U tulsajobspot -d tulsajobspot -c "SELECT id, job_title, created_at FROM joblistings WHERE approved=false ORDER BY created_at DESC LIMIT 20;"
```

## Dependencies

Install dependencies with:

```bash
pip install -r requirements.txt
```

Additional packages used by specific scrapers but not in requirements.txt: `mammoth`, `PyPDF2`.

ChromeDriver must be available on PATH (or placed in the scraper directory — it is gitignored).

## Architecture

### Two Scraper Generations

**Gen 1 (monolithic):** Contains its own `DatabaseManager` class with all SQL inline. Does not import from shared utilities. Identified by the absence of `from db_connection import` at the top.

**Gen 2 (modular):** Imports from the shared utility files in the repo root. Fixes to shared utilities automatically benefit these scrapers.

Most scrapers are currently Gen 1. When converting or updating a Gen 1 scraper, apply fixes individually to that file.

### Shared Utility Modules (`utils/`)

| File | Purpose |
|------|---------|
| `utils/db_connection.py` | `get_database_connection()`, `test_connection()`, `execute_with_retry()` |
| `utils/posting_operations.py` | `store_job_listing()`, `check_existing_job_by_url()`, `load_active_jobs_cache()`, `mark_stale_jobs_closed()` |
| `utils/company_operations.py` | `get_or_create_company()`, `get_or_create_company_site()` |
| `utils/date_utilities.py` | `parse_relative_date()`, `parse_workday_date()`, `normalize_date_string()` |
| `utils/utility_methods.py` | `setup_logging()` — creates `{company}_scraper.log` per run |
| `utils/selenium_config.py` | `SeleniumConfig.get_chrome_options()` — headless, anti-detection, eager load |

### Scraper Structure

Each scraper targets one company on one job board platform. Scrapers are organized by platform:

- `adp/` — ADP Workforce Now (hybrid DOM + API)
- `workday/` — Workday (API + Selenium for detail pages)
- `paycom/` — Paycom (API + Selenium for salary extraction)
- `paylocity/` — Paylocity (requests + BeautifulSoup, no Selenium)
- `applitrack/` — Applitrack/Frontline (Selenium + DOCX/PDF document extraction via `mammoth`/`PyPDF2`)
- `ultipro/` — UltiPro/UKG (Selenium, `data-automation` selectors)
- `custom/` — One-off scrapers for company-specific job boards

Each folder contains a `template-*.py` that shows the expected pattern for new scrapers on that platform.

### Typical Scraper Flow

1. Fetch job list from employer API or parse job board HTML with Selenium
2. Filter to Tulsa-area locations
3. For each job: check `joblistings` by URL — if found, update timestamps and skip; if new, scrape detail page and insert
4. After all jobs processed: call `mark_stale_jobs_closed()`, update `company.last_full_scrape_completed`
5. Write summary row to `scrapinglog`

## Critical Database Rules

These have caused bugs in the past — check every scraper against them:

1. **Lowercase table names**: `joblistings`, `jobstatus`, `company`, `companysite`, `scrapinglog`, `functions`, `jobtype`. PascalCase names are wrong.
2. **Never set `approved=True`** in any INSERT. Omit the column — the DB default is `false`. Admin reviews the pending queue.
3. **Look up `job_status_id` by name**, never hardcode an integer: `SELECT id FROM jobstatus WHERE name = 'active'` (lowercase status names).
4. **No `city` text column on `companysite`** — it was removed. Use `city_id` (FK to `cities` table) only.
5. **Country/state IDs**: look up by name, never assume ID 1: `SELECT id FROM country WHERE iso_code_2 = 'US'`, `SELECT id FROM state WHERE name = 'Oklahoma'`.
6. **Update both `updated_at` and `last_scraped`** when a previously-seen job is confirmed still live.

## Updating an Existing Scraper (Checklist)

When converting a Gen 1 scraper or fixing schema issues:

1. Grep for `FROM `, `INSERT INTO`, `UPDATE ` — fix any capitalized table names
2. Remove `approved=True` or `approved=1` from INSERTs
3. Replace hardcoded `job_status_id = <int>` with a named subquery
4. Remove any `city` column from `companysite` INSERTs
5. Verify connection string reads from env vars (default to `tulsajobspot`)
6. Test on the server using the workflow above

# Using the Dry Run Wrapper

`dry_run.py` lets you test any Gen 2 scraper against the live site without writing anything to the database. Job listings that *would* have been inserted are written to a local text file instead. Use it to verify a new or updated scraper before running it for real.

## Usage

From the repo root on the server (after sourcing `env.sh`):

```bash
python dry_run.py workday/williams-workday-api-selenium-scrape.py
```

When it finishes, the output file path is printed:

```
DRY RUN COMPLETE — 4 job(s) captured
Output:  dry_run_williams-workday-api-selenium-scrape_20260614_143022.txt
```

Review it:

```bash
nano dry_run_williams-workday-api-selenium-scrape_20260614_143022.txt
```

### Other examples

```bash
python dry_run.py workday/greenheck-workday-api-selenium-scrape.py
python dry_run.py adp/ok-cancer-spec-adp-api-selenium.py
```

## Output file format

Each captured job looks like this:

```
──────────────────────────────────────────────────────────────────────
JOB 1 of 4
──────────────────────────────────────────────────────────────────────
Title                 Senior Pipeline Engineer
URL                   https://williams.wd5.myworkdayjobs.com/External/...
Posting ID            R0012345
Company ID            1172
Source                Williams Workday
City ID               42
Date Posted           2026-05-15
Job Type ID           1
Work Location ID      2
Function ID           8
Hash                  a3f9c...

DESCRIPTION
──────────────────────────────────────────────────────────────────────
<p>We are seeking a Senior Pipeline Engineer...</p>
```

Metadata fields appear first (title, URL, all resolved IDs), then the full HTML description at the bottom. This makes it easy to quickly scan titles and field mappings without scrolling through description text.

## What is and isn't intercepted

| Operation | Dry run behavior |
|-----------|-----------------|
| Selenium page scraping | Runs normally — real pages are fetched |
| Workday / ADP API calls | Run normally — real job data is returned |
| DB lookups (city, function, job type, company IDs) | Run normally — IDs in the output file are exactly what a live run would use |
| `joblistings` INSERT | **Intercepted** — job data captured to file, nothing written |
| Stale job UPDATE | **Intercepted** — no-op |
| Existing job check | **Intercepted** — always returns "not found" so every job is treated as new |
| `scrapinglog` INSERT | Runs on the cursor but is **rolled back** at the end |
| `company` timestamp UPDATE | Runs on the cursor but is **rolled back** at the end |

The database is left completely unchanged after a dry run.

## How it works

The wrapper uses two techniques:

**1. Module-level patching (for `posting_operations`)**

Python caches imported modules in `sys.modules`. `dry_run.py` imports `utils.posting_operations` and replaces its functions *before* loading the scraper. When the scraper then runs `from utils.posting_operations import store_job_listing`, it resolves the name from the already-loaded (patched) module and gets the mock version automatically.

**2. Transaction rollback (for everything else)**

The DB connection is forced into `autocommit=False` mode so all statements run inside a single transaction. A patched `close_connection` calls `conn.rollback()` before closing, undoing any writes that reached the cursor directly (scrapinglog inserts, company timestamp updates, etc.).

## Compatibility

Works with **Gen 2 scrapers** — those that import from `utils/db_connection`, `utils/posting_operations`, and `utils/company_operations`.

Gen 1 scrapers manage their own database connections internally and are not fully covered. On a Gen 1 scraper, the `store_job_listing` / `check_existing_job_by_url` / `mark_stale_jobs_closed` patches still apply if the scraper happens to use those names, but any inline SQL will not be intercepted or rolled back.

## After a successful dry run

When you're satisfied with the output, run the scraper normally:

```bash
python workday/williams-workday-api-selenium-scrape.py
```

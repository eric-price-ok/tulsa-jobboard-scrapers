#!/usr/bin/env python3
"""
dry_run.py — run any Gen 2 scraper without writing to the database.

All DB lookups (city IDs, function IDs, job type IDs, company IDs) work
normally so mapped field values reflect a real run. Job listings are captured
to a local text file instead of being inserted. Everything else (scrapinglog
entries, company timestamp updates) is rolled back at the end.

Usage:
    python dry_run.py workday/williams-workday-api-selenium-scrape.py
    python dry_run.py workday/greenheck-workday-api-selenium-scrape.py
    python dry_run.py adp/ok-cancer-spec-adp-api-selenium.py
"""

import sys
import importlib.util
from pathlib import Path
from datetime import datetime

# ──────────────────────────────────────────────────────────────────────────────
# Patch shared utils BEFORE loading the scraper module.
#
# Gen 2 scrapers do `from utils.posting_operations import store_job_listing`.
# Python resolves that name from sys.modules at import time. We load the
# module first and replace the attributes, so the scraper automatically
# picks up our mocked versions.
# ──────────────────────────────────────────────────────────────────────────────

_captured_jobs = []

import utils.posting_operations as _po
import utils.db_connection as _dbc
import utils.company_operations as _co


# ── posting_operations patches ─────────────────────────────────────────────

def _mock_store_job_listing(cursor, job_data, company_id, source_job_board):
    entry = dict(job_data)
    entry['_company_id'] = company_id
    entry['_source_job_board'] = source_job_board
    _captured_jobs.append(entry)
    fake_id = -(len(_captured_jobs))
    print(f"  [DRY RUN] Captured: {entry.get('job_title', '?')} (would be id={fake_id})")
    return fake_id

_po.store_job_listing         = _mock_store_job_listing
_po.check_existing_job_by_url = lambda cursor, url: None          # treat all jobs as new
_po.mark_stale_jobs_closed    = lambda cursor, company_id, logger=None: None  # no-op

# ── company_operations patches ─────────────────────────────────────────────
# get_or_create_company_site does an INSERT that is not a job listing write,
# so it isn't covered by the posting_operations patch above. Mock it so it
# doesn't execute any SQL and can't abort the transaction.

def _mock_get_or_create_company_site(cursor, company_id, location_name, city_id=None, logger=None):
    print(f"  [DRY RUN] Would create companysite shortname='{location_name}' for company {company_id}")
    return None

_co.get_or_create_company_site = _mock_get_or_create_company_site


# ── db_connection patches ───────────────────────────────────────────────────
# Wrap the connection so everything runs inside a transaction we roll back.
# This catches any cursor.execute writes that aren't in posting_operations
# (e.g. scrapinglog INSERTs, company timestamp UPDATEs).

_real_get_db = _dbc.get_database_connection
_real_close  = _dbc.close_connection


def _mock_get_database_connection():
    conn = _real_get_db()
    conn.autocommit = False   # override any True set inside get_database_connection
    return conn


def _mock_close_connection(conn):
    if conn:
        try:
            conn.rollback()
            print("[DRY RUN] DB transaction rolled back — no writes committed")
        except Exception:
            pass
    _real_close(conn)


_dbc.get_database_connection = _mock_get_database_connection
_dbc.close_connection        = _mock_close_connection


# ──────────────────────────────────────────────────────────────────────────────
# Output writer
# ──────────────────────────────────────────────────────────────────────────────

# Metadata fields to show before the description (label width = 22)
_META_FIELDS = [
    ('job_title',          'Title'),
    ('posting_url',        'URL'),
    ('posting_id',         'Posting ID'),
    ('_company_id',        'Company ID'),
    ('_source_job_board',  'Source'),
    ('city_id',            'City ID'),
    ('date_posted',        'Date Posted'),
    ('date_closed',        'Date Closed'),
    ('job_type_id',        'Job Type ID'),
    ('office_location_id', 'Work Location ID'),
    ('function',           'Function ID'),
    ('minimum_salary',     'Min Salary'),
    ('maximum_salary',     'Max Salary'),
    ('scraping_hash',      'Hash'),
]

_KNOWN_FIELDS = {f for f, _ in _META_FIELDS} | {'job_description'}


def write_output(scraper_path: str) -> str:
    scraper_name = Path(scraper_path).stem
    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file  = f"dry_run_{scraper_name}_{timestamp}.txt"

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("DRY RUN OUTPUT\n")
        f.write(f"Scraper:  {scraper_path}\n")
        f.write(f"Run date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Captured: {len(_captured_jobs)} job(s) — nothing written to DB\n")
        f.write("=" * 70 + "\n\n")

        for i, job in enumerate(_captured_jobs, 1):
            f.write("─" * 70 + "\n")
            f.write(f"JOB {i} of {len(_captured_jobs)}\n")
            f.write("─" * 70 + "\n")

            # Known metadata fields
            for field, label in _META_FIELDS:
                value = job.get(field)
                if value is not None:
                    f.write(f"{label:<22}{value}\n")

            # Any unexpected extra fields (future-proof)
            for key, value in job.items():
                if key not in _KNOWN_FIELDS and value is not None:
                    f.write(f"{key:<22}{value}\n")

            f.write("\n")

            # Description last — can be long HTML
            desc = job.get('job_description', '')
            if desc:
                f.write("DESCRIPTION\n")
                f.write("─" * 70 + "\n")
                f.write(desc)
                f.write("\n")

            f.write("\n")

    return output_file


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _load_and_run(scraper_path: str):
    path = Path(scraper_path)
    if not path.exists():
        raise FileNotFoundError(f"Scraper not found: {scraper_path}")

    spec   = importlib.util.spec_from_file_location("_dry_run_scraper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, 'main'):
        raise AttributeError(f"Scraper has no main() function: {scraper_path}")

    return module.main()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    scraper_path = sys.argv[1]

    print("=" * 70)
    print("DRY RUN MODE — DB writes intercepted, nothing will be committed")
    print(f"Scraper: {scraper_path}")
    print("=" * 70)
    print()

    try:
        _load_and_run(scraper_path)
    except Exception as e:
        import traceback
        print(f"[DRY RUN] Scraper error: {e}")
        traceback.print_exc()

    output_file = write_output(scraper_path)

    print()
    print("=" * 70)
    print(f"DRY RUN COMPLETE — {len(_captured_jobs)} job(s) captured")
    print(f"Output:  {output_file}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit(main())

#!/usr/bin/env python3
"""
quiktrip.py
QuikTrip custom job board scraper (Gen 2)

The search page is plain server-rendered HTML (a <table> of <tr class="data-row">
rows) — no JavaScript rendering required, so this uses requests/BeautifulSoup
instead of Selenium. Each result row duplicates its title/date/location markup
twice: once in td.colTitle's hidden "jobdetail-phone visible-phone" div (a
mobile-only copy, empty when rendered on desktop) and once in the real
td.colDate / td.colLocation cells. Selectors here are scoped to the latter to
avoid picking up the empty mobile duplicate.

QuikTrip's search returns jobs across many cities (not just the searched one),
so every row's location is matched against the served-cities table and
non-served jobs are skipped.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import (
    store_job_listing, load_active_jobs_cache, check_job_in_cache,
    update_job_verified_timestamp, mark_stale_jobs_closed,
)
from utils.company_operations import get_company_config_by_name
from utils.date_utilities import normalize_date_string
from utils.location_utilities import find_served_city, get_city_id
from utils.utility_methods import setup_logging

from urllib.parse import urljoin
import time
import hashlib
import re
import requests
from bs4 import BeautifulSoup
from typing import Dict, List, Optional

logger = setup_logging('QuikTrip')

COMPANY_NAME = 'QuikTrip'
SOURCE_JOB_BOARD = 'QT Custom Scraper'
BASE_URL = 'https://careers.quiktrip.com/'

# QuikTrip is primarily convenience-store retail, but also staffs corporate
# office functions (IT, accounting, etc.), warehouses, and its own trucking fleet.
_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'database',
        'system', 'network', 'security engineer', 'devops', 'cloud',
        'application', 'help desk', 'it support', 'cyber',
    ],
    'Operations': [
        'driver', 'cdl', 'truck', 'transport', 'logistics', 'fleet', 'dispatcher',
        'project manager', 'program manager', 'store manager', 'operations manager',
        'district manager', 'division manager',
    ],
    'Purchasing': [
        'warehouse', 'distribution', 'supply chain', 'inventory', 'shipping',
        'receiving', 'procurement', 'purchasing',
    ],
    'Hospitality': [
        'kitchen', 'cook', 'food service', 'chef', 'culinary',
    ],
    'Skilled Trades': [
        'maintenance', 'technician', 'mechanic', 'electrician', 'facilities',
        'construction', 'equipment operator',
    ],
    'Accounting': [
        'finance', 'financial', 'accounting', 'accountant', 'audit', 'payroll',
        'treasury',
    ],
    'Human Resources': [
        'human resources', 'hr', 'recruiter', 'talent', 'benefits',
    ],
    'Legal': [
        'legal', 'attorney', 'counsel', 'compliance', 'real estate',
    ],
    'Marketing': [
        'marketing', 'brand', 'communications', 'social media', 'advertising',
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'food safety',
    ],
    'Security': [
        'security', 'safety', 'loss prevention', 'asset protection',
    ],
    'Administrative': [
        'admin', 'administrative', 'coordinator', 'assistant', 'clerk', 'office',
    ],
    'Customer Support': [
        'customer service', 'cashier', 'clerk', 'assistant manager', 'team leader',
        'shift leader', 'attendant',
    ],
}


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    title_lower = (job_title or '').lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in title_lower:
                cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                    return result['id']
    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Other',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{job_title}' to function: Other (no match)")
        return result['id']
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    cursor.execute("""
        UPDATE company SET last_full_scrape_completed = CURRENT_TIMESTAMP WHERE id = %s
    """, (company_id,))
    logger.info(f"Updated last_full_scrape_completed for company {company_id}")


def _log_scraping_activity(cursor, company_id: int, stats: Dict):
    cursor.execute("""
        INSERT INTO scrapinglog (
            job_board, company_id, jobs_found, jobs_added, jobs_updated,
            jobs_skipped, errors, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        SOURCE_JOB_BOARD,
        company_id,
        stats.get('found', 0),
        stats.get('added', 0),
        stats.get('updated', 0),
        stats.get('skipped', 0),
        str(stats.get('errors', [])),
        'completed'
    ))


def _extract_row_metadata(row) -> Optional[Dict]:
    """Parse a single <tr class="data-row">. Title comes from the desktop
    (hidden-phone) copy in td.colTitle; date/location come from the sibling
    td.colDate / td.colLocation cells — the ONLY place they appear outside
    the empty mobile duplicate nested inside td.colTitle."""
    try:
        title_link = row.select_one('td.colTitle span.jobTitle.hidden-phone a.jobTitle-link')
        if not title_link:
            title_link = row.select_one('td.colTitle a.jobTitle-link')
        if not title_link:
            return None
        job_title = title_link.get_text(strip=True)
        href = title_link.get('href')
        posting_url = urljoin(BASE_URL, href) if href else None

        date_element = row.select_one('td.colDate span.jobDate')
        date_raw = date_element.get_text(strip=True) if date_element else None

        location_element = row.select_one('td.colLocation span.jobLocation')
        location_raw = location_element.get_text(separator=' ', strip=True) if location_element else ''

        # Location reads like "TULSA, OK, US, 74134 +17 more…" — only the city
        # (first comma segment) is compared against the served-cities table.
        # find_served_city lowercases both sides, so all-caps "TULSA" matches fine.
        city_segment = location_raw.split(',')[0].strip() if location_raw else ''
        city_name = find_served_city(city_segment)

        logger.info(f"Row: {job_title} - {location_raw} - {date_raw}")
        return {
            'job_title': job_title,
            'posting_url': posting_url,
            'date_posted_raw': date_raw,
            'date_posted': normalize_date_string(date_raw) if date_raw else None,
            'location_raw': location_raw,
            'city_name': city_name,
        }
    except Exception as e:
        logger.error(f"Error extracting row metadata: {e}")
        return None


class QuikTripScraper:

    def __init__(self, conn):
        self.conn = conn
        with self.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{COMPANY_NAME}' not found in database")
        self.company_id = self.company_config['id']

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
        })

    def get_job_listings(self, jobboard_url: str) -> List[Dict]:
        try:
            logger.info(f"Fetching job board: {jobboard_url}")
            response = self.session.get(jobboard_url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            row_elements = soup.select('tr.data-row')
            logger.info(f"Found {len(row_elements)} job rows")
            if len(row_elements) == 25:
                logger.warning("Exactly 25 rows found — verify this isn't a page-size cap on the live site")

            jobs = []
            for row in row_elements:
                job_data = _extract_row_metadata(row)
                if job_data:
                    jobs.append(job_data)

            logger.info(f"Successfully extracted {len(jobs)} job listings")
            return jobs
        except Exception as e:
            logger.error(f"Error loading job board: {e}")
            return []

    def get_job_description(self, job_url: str) -> str:
        try:
            logger.info(f"  Fetching job detail page: {job_url}")
            response = self.session.get(job_url, timeout=20)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            container = soup.select_one('div.jobDisplay')
            if container:
                text = re.sub(r'\s+', ' ', container.get_text(separator=' ', strip=True)).strip()
                if len(text) > 50:
                    logger.info(f"  Extracted description via div.jobDisplay: {len(text)} characters")
                    return text[:50000]

            # Generic fallback if jobDisplay isn't present for some reason
            for selector in ['main', 'article', 'div[role="main"]']:
                content = soup.select_one(selector)
                if content:
                    text = re.sub(r'\s+', ' ', content.get_text(separator=' ', strip=True)).strip()
                    if len(text) > 100:
                        logger.info(f"  Extracted description via fallback '{selector}': {len(text)} characters")
                        return text[:50000]

            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                    tag.decompose()
                text = re.sub(r'\s+', ' ', body.get_text(separator=' ', strip=True)).strip()
                if len(text) > 100:
                    logger.info(f"  Extracted description from body fallback: {len(text)} characters")
                    return text[:50000]

            logger.warning("  No meaningful job description found")
            return ""
        except Exception as e:
            logger.error(f"  Error fetching job detail page: {e}")
            return ""

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                logger.info("Step 1: Loading active jobs cache...")
                active_jobs_cache = load_active_jobs_cache(cursor, self.company_id)

                logger.info("Step 2: Getting job listings from QuikTrip search page...")
                all_jobs = self.get_job_listings(self.company_config['jobboard'])
                if not all_jobs:
                    raise Exception("No jobs retrieved from job board")
                stats['found'] = len(all_jobs)
                logger.info(f"  Retrieved {len(all_jobs)} total rows")

                logger.info("Step 3: Filtering to served cities and processing jobs...")
                for i, job in enumerate(all_jobs):
                    try:
                        title = job.get('job_title', 'Unknown')
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        if not job.get('posting_url'):
                            logger.warning("  No posting URL found, skipping")
                            stats['skipped'] += 1
                            continue

                        if not job.get('city_name'):
                            logger.info(f"  Location '{job.get('location_raw', '')}' not in served area, skipping")
                            stats['skipped'] += 1
                            continue

                        city_id = get_city_id(cursor, job['city_name'])
                        if not city_id:
                            logger.warning(f"  Served city '{job['city_name']}' not found in DB, skipping")
                            stats['skipped'] += 1
                            continue

                        existing_job_id = check_job_in_cache(job['posting_url'], active_jobs_cache)
                        if existing_job_id:
                            update_job_verified_timestamp(cursor, existing_job_id)
                            stats['updated'] += 1
                            continue

                        description = self.get_job_description(job['posting_url'])
                        if not description or len(description.strip()) < 50:
                            logger.warning("  Failed to get meaningful job content, skipping")
                            stats['skipped'] += 1
                            continue

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': job['posting_url'],
                            'date_posted': job.get('date_posted'),
                            'scraping_hash': self.create_scraping_hash(title, job['posting_url'], description),
                            'function': _map_job_to_function(cursor, title),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, self.company_id, SOURCE_JOB_BOARD)
                        logger.info(f"  Stored job ID: {job_id} (city: {job['city_name']})")
                        stats['added'] += 1
                        time.sleep(0.3)

                    except Exception as e:
                        error_msg = f"Error processing '{job.get('job_title', 'Unknown')}': {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                logger.info("Step 4: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, self.company_id, logger)

                logger.info("Step 5: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, self.company_id)

                logger.info("Step 6: Logging results...")
                _log_scraping_activity(cursor, self.company_id, stats)

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        self.session.close()


def main():
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = QuikTripScraper(conn)

        logger.info(f"Starting {COMPANY_NAME} job scraping...")
        results = scraper.scrape_jobs()

        logger.info("=== SCRAPING SUMMARY ===")
        logger.info(f"Jobs found:   {results['found']}")
        logger.info(f"Jobs added:   {results['added']}")
        logger.info(f"Jobs updated: {results['updated']}")
        logger.info(f"Jobs skipped: {results['skipped']}")
        logger.info(f"Errors:       {len(results['errors'])}")

        if results['errors']:
            logger.error("Errors encountered:")
            for error in results['errors']:
                logger.error(f"  - {error}")

    except Exception as e:
        logger.error(f"Script failed: {e}")
        return 1
    finally:
        if scraper:
            scraper.cleanup()
        close_connection(conn)

    return 0


if __name__ == "__main__":
    exit(main())

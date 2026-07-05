#!/usr/bin/env python3
"""
zeeco-lever-api-scrape.py
Zeeco Lever API scraper (Gen 2)

Zeeco's public careers page (a Duda/multiscreensite.com site) renders a JS
widget that pulls its job feed from Lever, and every "Apply" button links to
jobs.lever.co/zeeco/<id>/apply. Lever exposes that same feed through a public,
unauthenticated JSON API (https://api.lever.co/v0/postings/<slug>?mode=json),
so this hits that API directly instead of driving the branded widget with
Selenium — no location-dropdown interaction, no "more jobs" pagination, and
no ambiguity about which container holds the description.

Zeeco is a global company; the API returns postings from every office, so
each posting's location is matched against the served-cities table.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import (
    store_job_listing, load_active_jobs_cache, check_job_in_cache,
    update_job_verified_timestamp, mark_stale_jobs_closed,
)
from utils.company_operations import get_company_config_by_name
from utils.location_utilities import find_served_city, get_city_id
from utils.utility_methods import setup_logging

from urllib.parse import urlparse
from datetime import datetime
import hashlib
import re
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from typing import Dict, List, Optional

logger = setup_logging('Zeeco')

COMPANY_NAME = 'Zeeco'
SOURCE_JOB_BOARD = 'Zeeco Custom Scraper'

# Zeeco's own Lever site slug — not derivable from the multiscreensite.com
# jobboard URL stored in the DB, so it's hardcoded here.
LEVER_SITE_SLUG = 'zeeco'
LEVER_API_URL = f'https://api.lever.co/v0/postings/{LEVER_SITE_SLUG}?mode=json'

# Zeeco manufactures combustion/burner management equipment for oil & gas and
# other industrial clients. Matched against title + Lever team/department text.
_FUNCTION_KEYWORDS = {
    'Engineering': [
        'mechanical engineer', 'mechanical', 'combustion engineer', 'burner', 'thermal',
        'electrical engineer', 'electrical', 'controls engineer', 'instrumentation', 'plc',
        'applications engineer', 'application engineer', 'design engineer',
        'project engineer', 'process engineer', 'engineer',
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'analyst', 'database',
        'network', 'devops', 'cloud', 'help desk', 'it support', 'cyber', 'erp',
    ],
    'Manufacturing': [
        'manufacturing', 'production', 'assembly', 'fabrication', 'fabricator',
        'machinist', 'welder', 'welding', 'cnc', 'shop',
    ],
    'Skilled Trades': [
        'technician', 'maintenance', 'mechanic', 'electrician', 'field service',
        'commissioning',
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'inspector',
    ],
    'Purchasing': [
        'supply chain', 'purchasing', 'procurement', 'buyer', 'planner',
        'materials', 'inventory', 'logistics', 'shipping', 'receiving', 'warehouse',
    ],
    'Sales': [
        'sales', 'account manager', 'business development', 'account executive',
        'sales representative',
    ],
    'Customer Support': [
        'customer service', 'customer support', 'client services',
    ],
    'Operations': [
        'project manager', 'program manager', 'project coordinator',
    ],
    'Accounting': [
        'finance', 'financial', 'accounting', 'accountant', 'audit', 'payroll',
        'controller', 'treasury',
    ],
    'Human Resources': [
        'human resources', 'hr', 'recruiter', 'talent', 'benefits',
    ],
    'Marketing': [
        'marketing', 'brand', 'communications', 'social media',
    ],
    'Legal': [
        'legal', 'attorney', 'counsel', 'compliance', 'contract',
    ],
    'Administrative': [
        'admin', 'administrative', 'coordinator', 'assistant', 'clerk', 'office',
    ],
    'Security': [
        'security', 'safety', 'ehs', 'environmental health',
    ],
}


def _clean_html_description(html_content: str) -> str:
    """Keep structural HTML tags, strip all CSS classes and attributes."""
    KEEP_TAGS = {'p', 'br', 'strong', 'b', 'em', 'i', 'ul', 'ol', 'li',
                 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def serialize(node):
        if isinstance(node, NavigableString):
            return str(node)
        if isinstance(node, Tag):
            children = ''.join(serialize(child) for child in node.children)
            if node.name in KEEP_TAGS:
                return f'<{node.name}>{children}</{node.name}>'
            return children
        return ''

    soup = BeautifulSoup(html_content or '', 'html.parser')
    html = serialize(soup)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _map_job_to_function(cursor, job_title: str, team: str, department: str) -> Optional[int]:
    match_text = f"{job_title or ''} {team or ''} {department or ''}".lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in match_text:
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


class ZeecoScraper:

    def __init__(self, conn):
        self.conn = conn
        with self.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{COMPANY_NAME}' not found in database")
        self.company_id = self.company_config['id']

        parsed = urlparse(self.company_config['jobboard'])
        self.site_origin = f"{parsed.scheme}://{parsed.netloc}"

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json',
        })

    def get_job_postings(self) -> List[Dict]:
        try:
            logger.info(f"Fetching postings from Lever API: {LEVER_API_URL}")
            response = self.session.get(LEVER_API_URL, timeout=20)
            response.raise_for_status()
            postings = response.json()
            logger.info(f"Retrieved {len(postings)} total postings")
            return postings
        except Exception as e:
            logger.error(f"Error fetching Lever postings: {e}")
            return []

    def build_posting_url(self, posting_id: str) -> str:
        return f"{self.site_origin}/career-description/{posting_id}"

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                logger.info("Step 1: Loading active jobs cache...")
                active_jobs_cache = load_active_jobs_cache(cursor, self.company_id)

                logger.info("Step 2: Fetching job postings from Lever...")
                postings = self.get_job_postings()
                if not postings:
                    raise Exception("No postings retrieved from Lever API")
                stats['found'] = len(postings)

                logger.info("Step 3: Filtering to served cities and processing jobs...")
                for i, posting in enumerate(postings):
                    try:
                        title = posting.get('text', 'Unknown')
                        categories = posting.get('categories') or {}
                        location_raw = categories.get('location') or ''
                        logger.info(f"Processing job {i+1}/{len(postings)}: {title} ({location_raw})")

                        city_segment = location_raw.split(',')[0].strip() if location_raw else ''
                        city_name = find_served_city(city_segment)
                        if not city_name:
                            logger.info(f"  Location '{location_raw}' not in served area, skipping")
                            stats['skipped'] += 1
                            continue

                        city_id = get_city_id(cursor, city_name)
                        if not city_id:
                            logger.warning(f"  Served city '{city_name}' not found in DB, skipping")
                            stats['skipped'] += 1
                            continue

                        posting_id = posting.get('id')
                        if not posting_id:
                            logger.warning("  No posting id found, skipping")
                            stats['skipped'] += 1
                            continue

                        posting_url = self.build_posting_url(posting_id)

                        existing_job_id = check_job_in_cache(posting_url, active_jobs_cache)
                        if existing_job_id:
                            update_job_verified_timestamp(cursor, existing_job_id)
                            stats['updated'] += 1
                            continue

                        description = _clean_html_description(posting.get('description', ''))
                        if not description or len(description.strip()) < 50:
                            logger.warning("  Insufficient description content, skipping")
                            stats['skipped'] += 1
                            continue

                        created_at = posting.get('createdAt')
                        date_posted = datetime.fromtimestamp(created_at / 1000) if created_at else None

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': posting_url,
                            'date_posted': date_posted,
                            'scraping_hash': self.create_scraping_hash(title, posting_url, description),
                            'function': _map_job_to_function(
                                cursor, title, categories.get('team'), categories.get('department')
                            ),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, self.company_id, SOURCE_JOB_BOARD)
                        logger.info(f"  Stored job ID: {job_id} (city: {city_name})")
                        stats['added'] += 1

                    except Exception as e:
                        error_msg = f"Error processing '{posting.get('text', 'Unknown')}': {e}"
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
        scraper = ZeecoScraper(conn)

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

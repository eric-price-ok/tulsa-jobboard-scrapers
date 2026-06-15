#!/usr/bin/env python3
"""
Bank of Oklahoma Job Scraper
Scrapes jobs from BOK careers site and stores in tulsajobspot database
"""

import requests
from bs4 import BeautifulSoup
import logging
import hashlib
import time
import re
from datetime import date
from typing import Dict, List, Optional

from utils.db_connection import get_database_connection
from utils.posting_operations import (
    store_job_listing, load_active_jobs_cache, check_job_in_cache,
    update_job_verified_timestamp, mark_stale_jobs_closed
)
from utils.company_operations import get_company_config_by_name
from utils.utility_methods import setup_logging
from utils.location_utilities import find_served_city, match_location_to_city_id
from utils.date_utilities import normalize_date_string


class DatabaseManager:
    """Handles PostgreSQL database operations"""

    def __init__(self):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}
        self.logger = logging.getLogger(__name__)

    def load_active_jobs_cache(self, company_id: int):
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)

    def check_existing_job(self, job_url: str) -> Optional[int]:
        return check_job_in_cache(job_url, self.active_jobs_cache)

    def update_job_verified_timestamp(self, job_id: int):
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        with self.conn.cursor() as cursor:
            enhanced = job_data.copy()
            enhanced['company_id'] = company_id
            return store_job_listing(cursor, enhanced, company_id, 'Bank of Oklahoma')

    def _map_job_function(self, job_title: str) -> Optional[int]:
        if not job_title:
            return None
        job_title_lower = job_title.lower()
        function_keywords = {
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data',
                'analyst', 'database', 'system', 'network', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'solutions architect',
                'enterprise architect'
            ],
            'Finance': [
                'finance', 'financial', 'accounting', 'accountant', 'treasury',
                'controller', 'audit', 'banking', 'credit', 'loan'
            ],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Sales': ['sales', 'account manager', 'business development', 'revenue', 'relationship manager'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
            'Operations': ['operations', 'ops', 'supply chain', 'logistics', 'process', 'facility'],
            'Project Management': ['project manager', 'program manager', 'scrum master', 'project coordinator'],
            'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client', 'banker', 'teller'],
            'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
        }
        with self.conn.cursor() as cursor:
            for function_name, keywords in function_keywords.items():
                if any(kw in job_title_lower for kw in keywords):
                    cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                        return result['id']
        self.logger.info(f"  No function match for '{job_title}'")
        return None

    def get_job_type_id(self, name: str) -> Optional[int]:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM jobtype WHERE name = %s", (name,))
            result = cursor.fetchone()
            return result['id'] if result else None

    def get_office_location_id(self, name: str) -> Optional[int]:
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM officelocations WHERE name = %s", (name,))
            result = cursor.fetchone()
            return result['id'] if result else None

    def update_company_scrape_completed(self, company_id: int):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE company
                SET last_full_scrape_completed = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")

    def log_scraping_activity(self, job_board: str, stats: Dict):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO scrapinglog (
                    job_board, jobs_found, jobs_added, jobs_updated,
                    jobs_skipped, errors, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                job_board,
                stats.get('found', 0),
                stats.get('added', 0),
                stats.get('updated', 0),
                stats.get('skipped', 0),
                str(stats.get('errors', [])),
                'completed'
            ))


class BOKJobScraper:
    """Bank of Oklahoma job scraper"""
    COMPANY_NAME = 'Bank of Oklahoma'
    SEARCH_URL = 'https://jobs.bokf.com/search/'
    JOBBOARD_URL = 'https://jobs.bokf.com'
    MAX_PAGES = 20

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        self.company_id = self.company_config['id']
        self.logger = setup_logging(self.company_config['name'])
        self.db.logger = self.logger
        self.default_job_type_id = self.db.get_job_type_id('Full-time')
        self.default_office_location_id = self.db.get_office_location_id('On-site')

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
        })

    def _extract_location_from_row(self, row) -> str:
        """Extract location text from a job listing row using multiple strategies."""
        # Strategy 1: span.jobLocation — confirmed BOK/Taleo pattern
        loc_span = row.find('span', class_='jobLocation')
        if loc_span:
            return loc_span.get_text(strip=True)

        # Strategy 2: any span with class containing "location"
        loc_span = row.find('span', class_=re.compile(r'location', re.I))
        if loc_span:
            return loc_span.get_text(strip=True)

        # Strategy 3: td with a class containing "location"
        loc_td = row.find('td', class_=re.compile(r'location', re.I))
        if loc_td:
            return loc_td.get_text(strip=True)

        # Strategy 4: any td whose text looks like "City, ST"
        for td in row.find_all('td'):
            text = td.get_text(strip=True)
            if re.search(r',\s*[A-Z]{2}$', text):
                return text

        return ''

    def _extract_deadline_from_row(self, row) -> Optional[date]:
        """Extract application deadline date from a job listing row."""
        # Strategy 1: span whose text is/contains "Application Deadline" — date is next sibling
        for span in row.find_all('span'):
            if 'application deadline' in span.get_text(strip=True).lower():
                for sib in span.next_siblings:
                    sib_text = sib.get_text(strip=True) if hasattr(sib, 'get_text') else str(sib).strip()
                    if sib_text:
                        parsed = normalize_date_string(sib_text)
                        if parsed:
                            return parsed.date()
                        break

        # Strategy 2: regex scan each td for "Application Deadline <date>"
        for td in row.find_all('td'):
            td_text = td.get_text(separator=' ', strip=True)
            match = re.search(
                r'application\s+deadline[\s:]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\w+ \d{1,2},?\s*\d{4})',
                td_text, re.I
            )
            if match:
                parsed = normalize_date_string(match.group(1))
                if parsed:
                    return parsed.date()

        return None

    def get_job_listings(self) -> List[Dict]:
        """
        Fetch all Tulsa-metro job listings across all pages.
        Filters out jobs not in served cities using find_served_city().
        Stops when a page returns no new URLs (end of results).
        """
        all_jobs = []
        seen_urls = set()
        limit = 25

        for page in range(self.MAX_PAGES):
            startrow = page * limit
            self.logger.info(f"Fetching page {page + 1} (startrow={startrow})...")

            try:
                response = self.session.get(
                    self.SEARCH_URL,
                    params={
                        'q': '',
                        'location': 'Tulsa',
                        'sortColumn': 'referencedate',
                        'sortDirection': 'desc',
                        'startrow': startrow
                    }
                )
                response.raise_for_status()
            except Exception as e:
                self.logger.error(f"Error fetching page {page + 1}: {e}")
                break

            soup = BeautifulSoup(response.content, 'html.parser')

            # Taleo search results: each job is a <tr class="jobslisting ...">
            job_rows = soup.find_all('tr', class_=re.compile(r'jobslisting', re.I))
            if not job_rows:
                # Fall back: walk up from each link to the enclosing <tr>
                job_rows = []
                for link in soup.find_all('a', class_='jobTitle-link'):
                    tr = link.find_parent('tr')
                    if tr:
                        job_rows.append(tr)

            if not job_rows:
                self.logger.info("No job rows found — end of results.")
                break

            self.logger.info(f"Found {len(job_rows)} job rows on page {page + 1}")
            new_urls_this_page = 0

            for row in job_rows:
                link = row.find('a', class_='jobTitle-link')
                if not link:
                    continue

                href = link.get('href', '')
                job_url = (self.JOBBOARD_URL + href) if href.startswith('/') else href
                if not job_url:
                    continue

                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)
                new_urls_this_page += 1

                location_text = self._extract_location_from_row(row)
                city_name = find_served_city(location_text)

                if not city_name:
                    self.logger.info(
                        f"  Skipping '{link.get_text(strip=True)}' "
                        f"— location '{location_text}' not in served cities"
                    )
                    continue

                with self.db.conn.cursor() as cursor:
                    _, city_id = match_location_to_city_id(cursor, location_text)

                deadline = self._extract_deadline_from_row(row)
                all_jobs.append({
                    'title': link.get_text(strip=True),
                    'url': job_url,
                    'location': city_name,
                    'city_id': city_id,
                    'date_closed': deadline,
                })
                self.logger.info(f"  Queued: '{link.get_text(strip=True)}' ({city_name})")

            if new_urls_this_page == 0:
                self.logger.info("No new URLs on this page — end of results.")
                break

            time.sleep(3)

        self.logger.info(f"Total Tulsa-metro jobs queued: {len(all_jobs)}")
        return all_jobs

    def get_job_description(self, job_url: str) -> str:
        """Download job detail page and extract the relevant description content."""
        try:
            self.logger.info(f"  Fetching job description: {job_url}")
            response = self.session.get(job_url)
            response.raise_for_status()
            return self._extract_job_content(response.text)
        except Exception as e:
            self.logger.error(f"  Error fetching job content: {e}")
            return ""

    def _extract_job_content(self, html: str) -> str:
        """
        Extract content from div.jobColumnOne, stopping before the
        'Advertising Source' h2 tag.
        """
        soup = BeautifulSoup(html, 'html.parser')

        column = soup.find('div', class_='jobColumnOne')
        if not column:
            self.logger.warning("  div.jobColumnOne not found — falling back to full body text")
            body = soup.find('body')
            return body.get_text(separator=' ', strip=True) if body else ''

        # Remove everything from the 'Advertising Source' h2 onward
        for tag in column.find_all('h2'):
            if 'advertising source' in tag.get_text(strip=True).lower():
                for sibling in list(tag.find_next_siblings()):
                    sibling.decompose()
                tag.decompose()
                break

        return column.get_text(separator=' ', strip=True)

    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            self.logger.info("Step 1: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            self.logger.info("Step 2: Getting job listings from BOK careers...")
            job_listings = self.get_job_listings()
            if not job_listings:
                raise Exception("No Tulsa-metro jobs retrieved from job board")

            stats['found'] = len(job_listings)
            self.logger.info(f"✓ Found {len(job_listings)} Tulsa-metro jobs")

            for i, job in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job['title']}")

                    existing_id = self.db.check_existing_job(job['url'])
                    if existing_id:
                        self.db.update_job_verified_timestamp(existing_id)
                        stats['updated'] += 1
                        continue

                    description = self.get_job_description(job['url'])
                    if not description or len(description.strip()) < 100:
                        self.logger.warning("  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue

                    scraping_hash = hashlib.md5(
                        f"{job['title']}{job['url']}{description}".encode('utf-8')
                    ).hexdigest()

                    job_data = {
                        'job_title': job['title'],
                        'posting_url': job['url'],
                        'job_description': description,
                        'scraping_hash': scraping_hash,
                        'function': self.db._map_job_function(job['title']),
                        'city_id': job['city_id'],
                        'job_type_id': self.default_job_type_id,
                        'office_location_id': self.default_office_location_id,
                        'date_posted': date.today(),
                        'date_closed': job.get('date_closed'),
                    }

                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ✓ Stored job with ID: {job_id}")
                    stats['added'] += 1

                    time.sleep(1)

                except Exception as e:
                    error_msg = f"Error processing job '{job.get('title', 'Unknown')}': {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1

            self.logger.info("Step 4: Marking stale jobs as closed...")
            with self.db.conn.cursor() as cursor:
                mark_stale_jobs_closed(cursor, self.company_id, self.logger)

            self.logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)

            self.logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('Bank of Oklahoma', stats)

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            self.logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats


def main():
    scraper = None
    try:
        db_manager = DatabaseManager()
        scraper = BOKJobScraper(db_manager)

        scraper.logger.info("Starting Bank of Oklahoma job scraping...")
        results = scraper.scrape_jobs()

        scraper.logger.info("=== SCRAPING SUMMARY ===")
        scraper.logger.info(f"Jobs found: {results['found']}")
        scraper.logger.info(f"Jobs added: {results['added']}")
        scraper.logger.info(f"Jobs updated: {results['updated']}")
        scraper.logger.info(f"Jobs skipped: {results['skipped']}")
        scraper.logger.info(f"Errors: {len(results['errors'])}")

        if results['errors']:
            scraper.logger.error("Errors encountered:")
            for error in results['errors']:
                scraper.logger.error(f"  - {error}")

    except Exception as e:
        if scraper and hasattr(scraper, 'logger'):
            scraper.logger.error(f"Script failed: {e}")
        else:
            print(f"Script failed: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())

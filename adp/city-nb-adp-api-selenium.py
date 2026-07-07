#!/usr/bin/env python3
"""
city-nb-adp-api-selenium.py
City National Bank ADP Job Board Scraper — Gen 2
API-based job list + Selenium detail page scraping for description, type, date, and salary
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
import hashlib
import re
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import requests

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import check_existing_job_by_url, store_job_listing, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.utility_methods import normalize_job_type, setup_logging, parse_salary_text
from utils.selenium_config import SeleniumConfig
from utils.location_utilities import find_served_city, get_city_id
from utils.date_utilities import normalize_date_string

# HTML tags preserved in cleaned job descriptions
_ALLOWED_TAGS = {'b', 'strong', 'i', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br'}

_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data',
        'database', 'system', 'network', 'security', 'devops', 'cloud',
        'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile',
    ],
    'Accounting': [
        'finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller',
        'audit', 'bookkeeping', 'clerk', 'loan', 'credit', 'banking',
    ],
    'Customer Support': [
        'customer service', 'support', 'help desk', 'call center', 'client',
        'teller', 'banker', 'representative', 'relationship',
    ],
    'Sales': [
        'sales', 'account manager', 'business development', 'revenue',
        'loan officer', 'mortgage', 'commercial lending',
    ],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
    'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
    'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
    'Operations': [
        'operations', 'ops', 'supply chain', 'logistics', 'process', 'facility',
        'project manager', 'program manager', 'scrum master', 'project coordinator',
    ],
    'Administrative': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
    'Security': ['security', 'safety', 'guard', 'protection'],
    'Executive': ['manager', 'director', 'supervisor', 'lead', 'executive', 'president', 'vice president'],
}

logger = logging.getLogger(__name__)


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    """Map job title to function ID using banking-specific keywords."""
    title_lower = job_title.lower()
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
        logger.info(f"  No function match for '{job_title}' — using Other")
        return result['id']
    return None


def _map_job_type(cursor, raw_text: str) -> Optional[int]:
    """
    Map job type text from detail page span to job_type_id.
    Strips ADP employment status qualifiers ('Regular', 'Seasonal') before normalizing,
    so 'Regular Full-Time' correctly maps to 'Full-time'.
    """
    if not raw_text:
        return None
    cleaned = re.sub(r'^(regular|seasonal)\s+', '', raw_text.strip(), flags=re.IGNORECASE).strip()
    canonical = normalize_job_type(cleaned) or normalize_job_type(raw_text)
    if not canonical:
        logger.warning(f"  Could not map '{raw_text}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{raw_text}' to job type: {canonical}")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    cursor.execute(
        "UPDATE company SET last_full_scrape_completed = CURRENT_TIMESTAMP WHERE id = %s",
        (company_id,)
    )
    logger.info(f"Updated last_full_scrape_completed for company {company_id}")


def _log_scraping_activity(cursor, job_board: str, company_id: int, stats: Dict):
    cursor.execute("""
        INSERT INTO scrapinglog (
            job_board, company_id, jobs_found, jobs_added, jobs_updated,
            jobs_skipped, errors, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        job_board, company_id,
        stats.get('found', 0), stats.get('added', 0), stats.get('updated', 0),
        stats.get('skipped', 0), str(stats.get('errors', [])), 'completed'
    ))


class CNBJobScraper:
    """City National Bank ADP job scraper — Gen 2"""

    COMPANY_NAME = 'City National Bank'
    JOBBOARD_URL = (
        'https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html'
        '?cid=a45b7363-090d-4c4a-b534-67b8d33f2e6f&ccId=19000101_000001'
        '&type=MP&lang=en_US&selectedMenuKey=CareerCenter'
    )
    API_ENDPOINT = (
        'https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions'
    )
    CID = 'a45b7363-090d-4c4a-b534-67b8d33f2e6f'
    CCID = '19000101_000001'

    def __init__(self):
        self.conn = get_database_connection()
        self.logger = setup_logging(self.COMPANY_NAME)
        self.driver = None

        with self.conn.cursor() as cursor:
            self.company_id = get_or_create_company(cursor, {
                'name': self.COMPANY_NAME,
                'website': 'https://www.cnb-ok.com',
                'jobboard': self.JOBBOARD_URL,
                'company_type_name': 'Private Company',
            })
            cursor.execute("SELECT id FROM officelocations WHERE name = %s", ('On-site',))
            result = cursor.fetchone()
            self.default_office_location_id = result['id'] if result else None

        self.logger.info(f"Company ID: {self.company_id} | On-site ID: {self.default_office_location_id}")

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
        })

        chrome_options = SeleniumConfig.get_chrome_options(headless=True)
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=chrome_options
        )
        SeleniumConfig.setup_driver_timeouts(self.driver)
        self.logger.info("Chrome WebDriver initialized")

    def get_job_listings_from_api(self) -> List[Dict]:
        """Fetch all job listings from CNB ADP API with pagination."""
        self.logger.info("Fetching job listings from ADP API...")
        all_jobs = []
        limit = 20
        offset = 0

        while True:
            params = {
                'cid': self.CID,
                'timeStamp': int(time.time() * 1000),
                'ccId': self.CCID,
                'lang': 'en_US',
                'locale': 'en_US',
                '$top': limit,
                '$skip': offset,
            }
            try:
                response = self.session.get(
                    self.API_ENDPOINT,
                    params=params,
                    headers={'Referer': self.JOBBOARD_URL}
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                self.logger.error(f"API fetch error at offset {offset}: {e}")
                break

            batch = data.get('jobRequisitions', [])
            if not batch:
                break
            all_jobs.extend(batch)
            self.logger.info(f"  Fetched {len(batch)} jobs (offset {offset})")
            if len(batch) < limit:
                break
            offset += limit
            time.sleep(0.5)

        self.logger.info(f"Total jobs from API: {len(all_jobs)}")
        return all_jobs

    def filter_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter API jobs to served cities; attach _city_id to each match."""
        filtered = []
        with self.conn.cursor() as cursor:
            for job in jobs:
                for location in job.get('requisitionLocations', []):
                    short_name = location.get('nameCode', {}).get('shortName', '').strip()
                    matched = find_served_city(short_name)
                    if matched:
                        job['_city_id'] = get_city_id(cursor, matched)
                        filtered.append(job)
                        self.logger.info(f"  ✓ {matched}: {job.get('requisitionTitle', '')}")
                        break
        self.logger.info(f"Tulsa-metro jobs: {len(filtered)}")
        return filtered

    def build_job_url(self, external_job_id: str) -> str:
        return (
            f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            f"?cid={self.CID}&ccId={self.CCID}&type=MP&lang=en_US"
            f"&selectedMenuKey=CareerCenter&jobId={external_job_id}"
        )

    def scrape_job_description(self, external_job_id: str) -> Dict:
        """
        Load detail page and extract:
          - description: cleaned HTML from div.job-description-data
          - job_type_raw: text from span.job-description-worker-catergory
          - date_posted: parsed from span.job-description-post-date
          - salary_text: raw text from span.job-description-salary
        """
        job_url = self.build_job_url(external_job_id)
        result = {'description': '', 'job_type_raw': '', 'date_posted': None, 'salary_text': ''}
        try:
            self.driver.get(job_url)
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'div.job-description-data'))
                )
            except TimeoutException:
                time.sleep(3)

            soup = BeautifulSoup(self.driver.page_source, 'html.parser')

            # Search the full page for metadata spans — they live outside div.job-description-data
            # on the ADP detail page (sidebar/header area). Class name typo is intentional.
            job_type_span = soup.find('span', class_='job-description-worker-catergory')
            if job_type_span:
                result['job_type_raw'] = job_type_span.get_text(strip=True)

            date_span = soup.find('span', class_='job-description-post-date')
            if date_span:
                result['date_posted'] = normalize_date_string(date_span.get_text(strip=True))

            # Salary span also carries 'hydrated' from ADP's web components
            salary_span = soup.find('span', class_='job-description-salary')
            if salary_span:
                result['salary_text'] = salary_span.get_text(strip=True)

            content = soup.find('div', class_='job-description-data')
            if not content:
                self.logger.warning(f"  div.job-description-data not found for job {external_job_id}")
                return result

            # Clean HTML: remove script/style, unwrap disallowed tags, strip all attributes
            for tag in content.find_all(['script', 'style', 'noscript']):
                tag.decompose()
            for tag in content.find_all(True):
                if tag.name not in _ALLOWED_TAGS:
                    tag.unwrap()
            for tag in content.find_all(True):
                tag.attrs = {}

            result['description'] = content.decode_contents()
            self.logger.info(
                f"  {len(result['description'])} chars"
                f" | type: {result['job_type_raw']!r}"
                f" | salary: {result['salary_text']!r}"
                f" | date: {result['date_posted']}"
            )
            return result

        except Exception as e:
            self.logger.warning(f"Error scraping job {external_job_id}: {e}")
            return result

    def scrape_jobs(self) -> Dict:
        """Main scraping method."""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}
        try:
            with self.conn.cursor() as cursor:
                self.logger.info("Step 1: Fetching jobs from ADP API...")
                all_jobs = self.get_job_listings_from_api()
                if not all_jobs:
                    raise Exception("No jobs returned from API")

                self.logger.info("Step 2: Filtering for Tulsa-metro jobs...")
                local_jobs = self.filter_tulsa_jobs(all_jobs)
                stats['found'] = len(local_jobs)
                if not local_jobs:
                    self.logger.warning("No Tulsa-metro jobs found")
                    return stats

                self.logger.info(f"Step 3: Processing {len(local_jobs)} jobs...")
                for i, job in enumerate(local_jobs):
                    try:
                        title = job.get('requisitionTitle', '')
                        self.logger.info(f"Processing job {i+1}/{len(local_jobs)}: {title}")

                        external_job_id = None
                        for field in job.get('customFieldGroup', {}).get('stringFields', []):
                            if field.get('nameCode', {}).get('codeValue') == 'ExternalJobID':
                                external_job_id = field.get('stringValue')
                                break
                        if not external_job_id:
                            self.logger.warning("  No ExternalJobID — skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = self.build_job_url(external_job_id)
                        existing_id = check_existing_job_by_url(cursor, job_url)
                        if existing_id:
                            stats['updated'] += 1
                            continue

                        job_detail = self.scrape_job_description(external_job_id)
                        description = job_detail['description']
                        if not description or len(description.strip()) < 50:
                            self.logger.warning("  Insufficient description — skipping")
                            stats['skipped'] += 1
                            continue

                        salary = parse_salary_text(job_detail['salary_text'])

                        date_posted = job_detail['date_posted']
                        if date_posted and hasattr(date_posted, 'date'):
                            date_posted = date_posted.date()

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': job_url,
                            'date_posted': date_posted,
                            'scraping_hash': hashlib.md5(
                                f"{title}{job_url}{description}".encode()
                            ).hexdigest(),
                            'function': _map_job_to_function(cursor, title),
                            'job_type_id': _map_job_type(cursor, job_detail['job_type_raw']),
                            'minimum_salary': salary['minimum_salary'],
                            'maximum_salary': salary['maximum_salary'],
                            'pay_frequency': salary['pay_frequency'],
                            'office_location_id': self.default_office_location_id,
                            'city_id': job.get('_city_id'),
                        }

                        job_id = store_job_listing(cursor, job_data, self.company_id, 'CNB ADP')
                        self.logger.info(f"  ✓ Stored job ID: {job_id}")
                        stats['added'] += 1
                        time.sleep(1.0)

                    except Exception as e:
                        error_msg = f"Error processing '{job.get('requisitionTitle', '?')}': {e}"
                        self.logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                self.logger.info("Step 4: Marking stale jobs closed...")
                mark_stale_jobs_closed(cursor, self.company_id)

                self.logger.info("Step 5: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, self.company_id)

                self.logger.info("Step 6: Logging results...")
                _log_scraping_activity(cursor, 'CNB ADP', self.company_id, stats)

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            self.logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except Exception:
                pass


def main():
    scraper = None
    try:
        scraper = CNBJobScraper()
        scraper.logger.info("Starting City National Bank ADP scrape...")
        results = scraper.scrape_jobs()

        scraper.logger.info("=== SCRAPING SUMMARY ===")
        scraper.logger.info(f"Jobs found:   {results['found']}")
        scraper.logger.info(f"Jobs added:   {results['added']}")
        scraper.logger.info(f"Jobs updated: {results['updated']}")
        scraper.logger.info(f"Jobs skipped: {results['skipped']}")
        scraper.logger.info(f"Errors:       {len(results['errors'])}")
        if results['errors']:
            for error in results['errors']:
                scraper.logger.error(f"  - {error}")

    except Exception as e:
        if scraper and hasattr(scraper, 'logger'):
            scraper.logger.error(f"Script failed: {e}")
        else:
            print(f"Script failed: {e}")
        return 1
    finally:
        if scraper:
            scraper.cleanup()
            if hasattr(scraper, 'conn') and scraper.conn:
                close_connection(scraper.conn)

    return 0


if __name__ == "__main__":
    exit(main())

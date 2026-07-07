#!/usr/bin/env python3
"""
bancfirst-adp-api-selenium.py
BancFirst Corporation ADP Job Board Scraper
Hybrid approach: DOM extraction for job list + API enrichment for metadata
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
from datetime import datetime
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import requests

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import check_existing_job_by_url, store_job_listing, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.utility_methods import normalize_job_type
from utils.selenium_config import SeleniumConfig
from utils.location_utilities import find_served_city, get_city_id
from utils.date_utilities import normalize_date_string

# HTML tags preserved in cleaned job descriptions
_ALLOWED_TAGS = {'b', 'strong', 'i', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br'}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bancfirst_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Banking-focused function keyword mapping
_FUNCTION_KEYWORDS = {
    'Accounting': [
        'finance', 'financial', 'treasury', 'controller', 'audit', 'loan', 'credit',
        'banking', 'loan officer', 'mortgage', 'commercial lending', 'credit analyst',
        'underwriter', 'accounting', 'accountant', 'bookkeeping', 'clerk', 'accounting clerk'
    ],
    'Customer Support': [
        'customer service', 'support', 'teller', 'banker', 'representative',
        'relationship', 'customer'
    ],
    'Administrative': [
        'admin', 'administrative', 'coordinator', 'assistant', 'manager', 'director',
        'supervisor', 'lead', 'executive', 'president', 'vice president', 'branch manager'
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'engineer', 'data', 'database',
        'system', 'network', 'security', 'devops', 'cloud', 'application', 'web', 'mobile',
        'qa', 'testing', 'it'
    ],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
    'Legal': [
        'legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract',
        'compliance officer'
    ],
    'Marketing': [
        'marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'
    ],
    'Security': ['security', 'safety', 'guard', 'protection'],
}


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    """Map job title to function ID using banking-specific keywords"""
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
        logger.info(f"  Mapped '{job_title}' to function: Other (no specific match)")
        return result['id']
    logger.warning(f"  Could not map '{job_title}' to any function")
    return None


def _map_job_type(cursor, work_level_code: str) -> Optional[int]:
    """Map ADP work level code to job_type_id via normalize_job_type"""
    canonical = normalize_job_type(work_level_code)
    if not canonical:
        logger.warning(f"  Could not map '{work_level_code}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{work_level_code}' to job type: {canonical}")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    """Update last_full_scrape_completed timestamp for company"""
    cursor.execute("""
        UPDATE company
        SET last_full_scrape_completed = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (company_id,))
    logger.info(f"Updated last_full_scrape_completed for company {company_id}")


def _log_scraping_activity(cursor, job_board: str, company_id: int, stats: Dict):
    """Log scraping results to scrapinglog table"""
    cursor.execute("""
        INSERT INTO scrapinglog (
            job_board, company_id, jobs_found, jobs_added, jobs_updated,
            jobs_skipped, errors, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        job_board,
        company_id,
        stats.get('found', 0),
        stats.get('added', 0),
        stats.get('updated', 0),
        stats.get('skipped', 0),
        str(stats.get('errors', [])),
        'completed'
    ))


class BancFirstJobScraper:
    """BancFirst ADP job scraper — DOM extraction + paginated API enrichment"""

    def __init__(self, conn):
        self.conn = conn
        self.driver = None
        self.session = requests.Session()

        self.company_config = {
            'name': 'BancFirst',
            'website': 'https://www.bancfirst.com/',
            'jobboard_url': 'https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1da3e70c-e60a-466c-a367-419990b1b80f&ccId=19000101_000001&type=MP&lang=en_US',
            'api_endpoint': 'https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions',
            'cid': '1da3e70c-e60a-466c-a367-419990b1b80f',
            'ccId': '19000101_000001'
        }

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        })

        self.setup_selenium()

    def setup_selenium(self):
        """Initialize Chrome WebDriver"""
        try:
            chrome_options = SeleniumConfig.get_chrome_options(headless=True)
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
            SeleniumConfig.setup_driver_timeouts(self.driver)
            logger.info("Chrome WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def extract_all_jobs_from_dom(self) -> List[Dict]:
        """Extract all jobs from DOM using View All button + scrolling"""
        try:
            logger.info("Loading job listings page...")
            self.driver.get(self.company_config['jobboard_url'])

            wait = WebDriverWait(self.driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(5)

            # Click View All button to expand full list
            try:
                view_all_button = self.driver.find_element(By.ID, "recruitment_careerCenter_showAllJobs")
                self.driver.execute_script("arguments[0].scrollIntoView(true);", view_all_button)
                time.sleep(2)
                self.driver.execute_script("arguments[0].click();", view_all_button)
                time.sleep(8)
                logger.info("Clicked View All button")
            except Exception as e:
                logger.warning(f"Could not click View All button: {e}")

            # Scroll to load all dynamically rendered jobs
            last_job_count = 0
            scroll_attempts = 0

            for scroll in range(25):
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(4)

                job_elements = self.driver.find_elements(By.CSS_SELECTOR, "sdf-link[id*='lblTitle_']")
                current_job_count = len(job_elements)
                logger.info(f"Scroll {scroll + 1}: Found {current_job_count} job elements")

                if current_job_count == last_job_count:
                    scroll_attempts += 1
                    if scroll_attempts >= 5:
                        break
                else:
                    scroll_attempts = 0
                    last_job_count = current_job_count

            # Extract title, external_job_id, and location from each job card
            job_links = self.driver.find_elements(By.CSS_SELECTOR, "sdf-link[id*='lblTitle_']")
            jobs_found = []

            logger.info(f"Extracting data from {len(job_links)} jobs...")

            for i, link in enumerate(job_links):
                try:
                    title = link.text.strip()
                    link_id = link.get_attribute('id')
                    external_job_id = link_id.replace('lblTitle_', '') if 'lblTitle_' in link_id else None

                    location = ""
                    try:
                        parent = link.find_element(By.XPATH, "./ancestor::div[contains(@class, 'current-openings-details')]")
                        location_elem = parent.find_element(By.CSS_SELECTOR, ".current-opening-location-item span")
                        location = location_elem.text.strip()
                    except Exception:
                        pass

                    if title and external_job_id:
                        jobs_found.append({
                            'title': title,
                            'external_job_id': external_job_id,
                            'location': location
                        })
                        logger.info(f"  Job {i+1}: {title} | {location}")

                except Exception as e:
                    logger.warning(f"Error extracting job {i+1}: {e}")

            logger.info(f"Extracted {len(jobs_found)} jobs from DOM")
            return jobs_found

        except Exception as e:
            logger.error(f"Error extracting jobs from DOM: {e}")
            return []

    def get_api_data_for_jobs(self) -> Dict[str, Dict]:
        """Fetch metadata (date, salary, job type) for all jobs via paginated API"""
        try:
            logger.info("Fetching API metadata with pagination...")

            api_job_data = {}
            limit = 20
            offset = 0

            while True:
                logger.info(f"Fetching API page at offset {offset}...")

                timestamp = int(time.time() * 1000)
                params = {
                    'cid': self.company_config['cid'],
                    'timeStamp': timestamp,
                    'ccId': self.company_config['ccId'],
                    'lang': 'en_US',
                    'locale': 'en_US',
                    '$top': limit,
                    '$skip': offset
                }

                response = self.session.get(
                    self.company_config['api_endpoint'],
                    params=params,
                    headers={'Referer': self.company_config['jobboard_url']}
                )
                response.raise_for_status()
                data = response.json()

                if 'jobRequisitions' not in data:
                    logger.warning("No jobRequisitions in API response")
                    break

                batch_jobs = data['jobRequisitions']
                logger.info(f"Retrieved {len(batch_jobs)} jobs in this batch")

                if not batch_jobs:
                    break

                for job in batch_jobs:
                    external_job_id = None
                    string_fields = job.get('customFieldGroup', {}).get('stringFields', [])
                    for field in string_fields:
                        if field.get('nameCode', {}).get('codeValue') == 'ExternalJobID':
                            external_job_id = field.get('stringValue')
                            break

                    if not external_job_id:
                        continue

                    # BancFirst stores posting date in customFieldGroup.dateFields
                    date_posted = None
                    date_fields = job.get('customFieldGroup', {}).get('dateFields', [])
                    for field in date_fields:
                        if field.get('nameCode', {}).get('codeValue') == 'PostingDate':
                            date_value = field.get('dateValue')
                            if date_value:
                                try:
                                    date_posted = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                                except Exception:
                                    logger.warning(f"Could not parse date {date_value}")
                            break

                    min_salary = max_salary = None
                    pay_grade_range = job.get('payGradeRange', {})
                    if pay_grade_range:
                        min_rate = pay_grade_range.get('minimumRate', {})
                        max_rate = pay_grade_range.get('maximumRate', {})
                        if min_rate and 'amountValue' in min_rate:
                            min_salary = min_rate['amountValue']
                        if max_rate and 'amountValue' in max_rate:
                            max_salary = max_rate['amountValue']

                    api_job_data[external_job_id] = {
                        'posting_id': job.get('clientRequisitionID'),
                        'date_posted': date_posted,
                        'minimum_salary': min_salary,
                        'maximum_salary': max_salary,
                        'work_level': job.get('workLevelCode', {}).get('shortName', '')
                    }

                if len(batch_jobs) < limit:
                    break

                offset += limit
                time.sleep(0.5)

            logger.info(f"Retrieved API metadata for {len(api_job_data)} jobs")
            return api_job_data

        except Exception as e:
            logger.error(f"Error fetching API data: {e}")
            return {}

    def filter_served_city_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter DOM-extracted jobs to those in a served city"""
        filtered = []
        logger.info(f"Filtering {len(jobs)} jobs for served cities...")

        for job in jobs:
            matched = find_served_city(job.get('location', ''))
            if matched:
                filtered.append(job)
                logger.info(f"  ✓ {matched}: {job['title']} at {job['location']}")

        logger.info(f"Found {len(filtered)} jobs in served cities")
        return filtered

    def build_job_url(self, external_job_id: str) -> str:
        """Build job detail URL using external job ID"""
        return (
            f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            f"?cid={self.company_config['cid']}"
            f"&ccId={self.company_config['ccId']}"
            f"&type=MP&lang=en_US&selectedMenuKey=CareerCenter"
            f"&jobId={external_job_id}"
        )

    def scrape_job_description(self, external_job_id: str) -> Dict:
        """
        Scrape job detail page.
        Returns dict with 'description' (cleaned HTML), 'job_type_raw', and 'date_posted'.
        """
        job_url = self.build_job_url(external_job_id)
        result = {'description': '', 'job_type_raw': '', 'date_posted': None}
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
            # on the ADP detail page. Class name typo is intentional — matches ADP HTML.
            job_type_span = soup.find('span', class_='job-description-worker-catergory')
            if job_type_span:
                result['job_type_raw'] = job_type_span.get_text(strip=True)

            date_span = soup.find('span', class_='job-description-post-date')
            if date_span:
                result['date_posted'] = normalize_date_string(date_span.get_text(strip=True))

            content = soup.find('div', class_='job-description-data')
            if not content:
                logger.warning(f"  div.job-description-data not found for job {external_job_id}")
                return result

            # Strip tags with unwanted content first
            for tag in content.find_all(['script', 'style', 'noscript']):
                tag.decompose()

            # Unwrap disallowed tags — preserves their text content
            for tag in content.find_all(True):
                if tag.name not in _ALLOWED_TAGS:
                    tag.unwrap()

            # Remove all attributes from remaining tags
            for tag in content.find_all(True):
                tag.attrs = {}

            result['description'] = content.decode_contents()
            logger.info(
                f"  Extracted description: {len(result['description'])} chars"
                f" | type: {result['job_type_raw']!r}"
                f" | date: {result['date_posted']}"
            )
            return result

        except Exception as e:
            logger.warning(f"Error scraping job description: {e}")
            return result

    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company ID
                logger.info("Step 1: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': self.company_config['website'],
                    'jobboard': self.company_config['jobboard_url'],
                    'company_type_name': 'Private Company'
                })
                logger.info(f"  Resolved company ID: {company_id}")

                # Step 2: Extract all jobs from DOM
                logger.info("Step 2: Extracting jobs from DOM...")
                all_jobs = self.extract_all_jobs_from_dom()
                if not all_jobs:
                    raise Exception("No jobs found in DOM")

                # Step 3: Get API metadata (date, salary, job type) for all jobs
                logger.info("Step 3: Fetching API metadata...")
                api_data = self.get_api_data_for_jobs()

                # Step 4: Filter for served cities
                logger.info("Step 4: Filtering for served cities...")
                local_jobs = self.filter_served_city_jobs(all_jobs)
                stats['found'] = len(local_jobs)

                if not local_jobs:
                    logger.warning("No jobs found in served cities")
                    return stats

                # Step 5: Process each job
                for i, job in enumerate(local_jobs):
                    try:
                        logger.info(f"Processing job {i+1}/{len(local_jobs)}: {job['title']}")

                        external_job_id = job['external_job_id']
                        job_url = self.build_job_url(external_job_id)

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        job_detail = self.scrape_job_description(external_job_id)
                        description = job_detail['description']
                        if not description or len(description.strip()) < 50:
                            logger.warning("  Insufficient job description, skipping")
                            stats['skipped'] += 1
                            continue

                        job_api = api_data.get(external_job_id, {})
                        city_id = get_city_id(cursor, find_served_city(job.get('location', '')))

                        date_posted = job_detail['date_posted']
                        if date_posted and hasattr(date_posted, 'date'):
                            date_posted = date_posted.date()

                        job_data = {
                            'job_title': job['title'],
                            'job_description': description,
                            'posting_url': job_url,
                            'date_posted': date_posted,
                            'posting_id': job_api.get('posting_id'),
                            'external_job_id': external_job_id,
                            'minimum_salary': job_api.get('minimum_salary'),
                            'maximum_salary': job_api.get('maximum_salary'),
                            'scraping_hash': hashlib.md5(
                                f"{job['title']}{job_url}{description}".encode()
                            ).hexdigest(),
                            'function': _map_job_to_function(cursor, job['title']),
                            'job_type_id': _map_job_type(cursor, job_detail['job_type_raw']),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, company_id, 'BancFirst ADP')
                        logger.info(f"  ✓ Stored job with ID: {job_id}")
                        stats['added'] += 1
                        time.sleep(1.0)

                    except Exception as e:
                        error_msg = f"Error processing {job['title']}: {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                # Step 6: Mark stale jobs as closed
                logger.info("Step 6: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 7: Update company scrape completion
                logger.info("Step 7: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 8: Log results
                logger.info("Step 8: Logging results...")
                _log_scraping_activity(cursor, 'BancFirst ADP', company_id, stats)

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        """Clean up resources"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


def main():
    """Main execution function"""
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = BancFirstJobScraper(conn)

        logger.info("Starting BancFirst ADP job scraping...")
        results = scraper.scrape_jobs()

        logger.info("=== SCRAPING SUMMARY ===")
        logger.info(f"Jobs found: {results['found']}")
        logger.info(f"Jobs added: {results['added']}")
        logger.info(f"Jobs updated: {results['updated']}")
        logger.info(f"Jobs skipped: {results['skipped']}")
        logger.info(f"Errors: {len(results['errors'])}")

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

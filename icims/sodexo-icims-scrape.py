#!/usr/bin/env python3
"""
sodexo-icims-scrape.py
Sodexo iCIMS job board scraper (Gen 2)

Job board URL is read from company.jobboard at runtime. The search_api
and base_url are derived from that URL automatically.

Uses Strategy A (location_query = 'Tulsa, OK') to filter at the API level
since Sodexo is a nationwide employer with a large job volume.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.date_utilities import normalize_date_string
from utils.location_utilities import TULSA_METRO_CITIES
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location
from typing import Tuple
from urllib.parse import urlparse

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
from bs4 import BeautifulSoup, NavigableString, Tag
from typing import Dict, List, Optional
import requests

logger = setup_logging('Sodexo')

# Cap on new jobs added per run. Set to None for production.
MAX_JOBS_ADDED = 5

_SALARY_PATTERNS = [
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:USD|per\s+year|annually|/year)', False),
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:/hour|per\s+hour|hourly)',        True),
    (r'Salary\s+Range:?\s*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',                    False),
]


def _parse_salary_from_text(text: str) -> Tuple[Optional[float], Optional[float]]:
    for pattern, is_hourly in _SALARY_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                min_sal = float(match.group(1).replace(',', ''))
                max_sal = float(match.group(2).replace(',', ''))
                if is_hourly:
                    min_sal *= 2080
                    max_sal *= 2080
                return min_sal, max_sal
            except ValueError:
                continue
    return None, None


_FUNCTION_KEYWORDS = {
    'Customer Service': ['customer service', 'support', 'client', 'guest', 'hospitality'],
    'Skilled Labor': [
        'cook', 'chef', 'culinary', 'food service', 'dietary', 'cashier',
        'custodian', 'janitor', 'housekeeper', 'laundry', 'maintenance',
        'technician', 'mechanic', 'driver', 'delivery',
    ],
    'Security': ['security', 'safety', 'guard', 'officer'],
    'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'receptionist'],
    'Management': ['manager', 'director', 'supervisor', 'lead', 'general manager'],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'benefits'],
    'Finance': ['finance', 'financial', 'accounting', 'accountant', 'audit', 'payroll'],
    'Information Technology': [
        'software', 'developer', 'data', 'system', 'network', 'it ',
        'technology', 'cyber', 'devops', 'cloud',
    ],
    'Marketing': ['marketing', 'brand', 'communications', 'social media'],
    'Sales': ['sales', 'account manager', 'business development'],
    'Project Management': ['project manager', 'program manager', 'operations manager'],
}


def _clean_html_description(element) -> str:
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

    html = serialize(element)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
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
        logger.info(f"  Mapped '{job_title}' to function: Other (default)")
        return result['id']
    logger.warning(f"  Could not map '{job_title}' to any function")
    return None


def _map_job_type(cursor, time_type: str) -> Optional[int]:
    canonical = normalize_job_type(time_type)
    if not canonical:
        logger.warning(f"  Could not map job type '{time_type}'")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped job type '{time_type}' -> '{canonical}'")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _map_remote_type_to_office_location(cursor, remote_type: str) -> Optional[int]:
    canonical = normalize_work_location(remote_type)
    if not canonical:
        logger.warning(f"  Could not map work location '{remote_type}'")
        return None
    cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped work location '{remote_type}' -> '{canonical}' (id: {result['id']})")
        return result['id']
    logger.warning(f"  Work location '{canonical}' not found in officelocations table")
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    cursor.execute("""
        UPDATE company SET last_full_scrape_completed = CURRENT_TIMESTAMP WHERE id = %s
    """, (company_id,))
    logger.info(f"Updated last_full_scrape_completed for company {company_id}")


def _log_scraping_activity(cursor, job_board: str, company_id: int, stats: Dict):
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
        'completed',
    ))


class SeleniumJobScraper:

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()

    def setup_driver(self):
        try:
            chrome_options = SeleniumConfig.get_chrome_options(headless=self.headless)
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
            SeleniumConfig.setup_driver_timeouts(self.driver)
            logger.info("Selenium WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def get_job_content(self, job_url: str, timeout=12) -> str:
        try:
            logger.info(f"  Loading detail page: {job_url}")
            self.driver.get(job_url)
            wait = WebDriverWait(self.driver, timeout)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning("  Body tag not found within timeout")
                return ""
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, (
                        '.iCIMS_JobContent, #jobDescriptionText, '
                        '[data-field="jobDescriptionValue"], .job-header-description'
                    ))
                ))
                time.sleep(0.5)
            except TimeoutException:
                time.sleep(1.5)
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
        except TimeoutException:
            logger.warning("  Timeout waiting for detail page to load")
            return self.driver.page_source if self.driver else ""
        except Exception as e:
            logger.error(f"  Error loading detail page: {e}")
            return ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class SodexoScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        self.company_config = {
            'name':              'Sodexo',
            'website':           'https://www.sodexo.com',
            'company_type_name': 'Public Company',
            'source_job_board':  'Sodexo iCIMS',
            'location_query':    '-12820-',   # iCIMS location ID for Tulsa, OK
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'en-US,en;q=0.5',
            'X-Requested-With': 'XMLHttpRequest',
            'DNT': '1',
        })

    def establish_session(self, jobboard_url: str) -> bool:
        try:
            logger.info("Establishing session with Sodexo careers page...")
            response = self.session.get(jobboard_url)
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False

    def get_job_listings(self, search_api: str, base_url: str) -> List[Dict]:
        all_jobs = []
        start_index = 0
        page_size = 20
        total_results = None

        while True:
            try:
                params = {
                    'ss':             '1',
                    'searchRelation': 'keyword_all',
                    'searchKeyword':  '',
                    'searchLocation': self.company_config['location_query'],
                    'startIndex':     start_index,
                    'maxResults':     page_size,
                }
                logger.info(f"Fetching jobs with startIndex={start_index}")

                response = self.session.get(
                    search_api,
                    params=params,
                    headers={
                        'Referer': search_api,
                        'Origin':  base_url,
                    }
                )
                response.raise_for_status()
                try:
                    data = response.json()
                except Exception as json_err:
                    logger.error(
                        f"JSON parse failed: {json_err}\n"
                        f"Response status: {response.status_code}\n"
                        f"Response preview: {response.text[:500]}"
                    )
                    break

                jobs = (
                    data.get('searchResults')
                    or data.get('jobs')
                    or data.get('results')
                    or []
                )
                if total_results is None:
                    total_results = (
                        data.get('totalCount')
                        or data.get('total')
                        or len(jobs)
                    )
                    logger.info(f"Total jobs available: {total_results}")

                all_jobs.extend(jobs)
                logger.info(f"Retrieved {len(jobs)} jobs. Total so far: {len(all_jobs)}")

                start_index += page_size
                if start_index >= total_results or len(jobs) == 0:
                    break

                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error fetching jobs at startIndex={start_index}: {e}")
                break

        return all_jobs

    def _build_job_url(self, job: Dict, base_url: str) -> Optional[str]:
        raw_url = job.get('url') or job.get('detailUrl') or job.get('applyUrl', '')
        if raw_url:
            if raw_url.startswith('http'):
                return raw_url
            return f"{base_url}{raw_url}"
        job_id = job.get('id') or job.get('jobId')
        if job_id:
            return f"{base_url}/jobs/{job_id}/job"
        return None

    def _is_tulsa_location(self, location_text: str) -> bool:
        if not location_text:
            return False
        lower = location_text.lower()
        return any(city.lower() in lower for city in TULSA_METRO_CITIES)

    def extract_job_content(self, cursor, html_content: str) -> Tuple[str, Dict]:
        extracted = {
            'posting_id':         None,
            'time_type':          None,
            'office_location_id': None,
            'minimum_salary':     None,
            'maximum_salary':     None,
        }

        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            for label_text in ['job number', 'job id', 'requisition']:
                label = soup.find(string=re.compile(label_text, re.IGNORECASE))
                if label:
                    parent = label.find_parent()
                    if parent:
                        sibling = parent.find_next_sibling()
                        if sibling:
                            extracted['posting_id'] = sibling.get_text(strip=True)
                            logger.info(f"  Extracted posting ID: {extracted['posting_id']}")
                            break

            for dt in soup.find_all('dt'):
                label = dt.get_text(strip=True).lower()
                dd = dt.find_next_sibling('dd')
                if not dd:
                    continue
                value = dd.get_text(strip=True)

                if re.search(r'job\s*type|employment\s*type|schedule', label):
                    extracted['time_type'] = value
                    logger.info(f"  Job type (raw): '{value}'")

                elif re.search(r'remote|work\s*location|work\s*type', label):
                    office_location_id = _map_remote_type_to_office_location(cursor, value)
                    if office_location_id:
                        extracted['office_location_id'] = office_location_id

                elif re.search(r'salary|pay\s*range|compensation', label):
                    min_sal, max_sal = _parse_salary_from_text(value)
                    if min_sal:
                        extracted['minimum_salary'] = min_sal
                        extracted['maximum_salary'] = max_sal

            if not extracted['minimum_salary']:
                min_sal, max_sal = _parse_salary_from_text(soup.get_text())
                if min_sal:
                    extracted['minimum_salary'] = min_sal
                    extracted['maximum_salary'] = max_sal

            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            description = ""
            for selector in [
                '.iCIMS_JobContent',
                '#jobDescriptionText',
                '[data-field="jobDescriptionValue"]',
                '.job-header-description',
                '.iCIMS_Expandable_Container',
                '[role="main"]',
                'main',
            ]:
                content = soup.select_one(selector)
                if content and len(content.get_text(strip=True)) > 100:
                    logger.info(f"  Extracted description using selector: {selector}")
                    description = _clean_html_description(content)
                    break

            if not description:
                body = soup.find('body')
                if body:
                    for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                        tag.decompose()
                    description = _clean_html_description(body)

            logger.info(f"  Description length: {len(description)} characters")
            return description, extracted

        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return html_content, extracted

    def download_job_details(self, cursor, job_url: str) -> Tuple[str, Dict]:
        html = self.selenium_scraper.get_job_content(job_url)
        if html:
            return self.extract_job_content(cursor, html)
        return "", {}

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company ID
                logger.info("Step 1: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name':              self.company_config['name'],
                    'website':           self.company_config['website'],
                    'company_type_name': self.company_config['company_type_name'],
                })
                logger.info(f"  Company ID: {company_id}")

                # Step 2: Look up job board URL from company record
                logger.info("Step 2: Looking up job board URL from company table...")
                cursor.execute("SELECT jobboard FROM company WHERE id = %s", (company_id,))
                row = cursor.fetchone()
                if not row or not row['jobboard']:
                    raise Exception(
                        f"No jobboard URL found for '{self.company_config['name']}' in company table. "
                        "Set company.jobboard before running this scraper."
                    )
                jobboard_url = row['jobboard']
                parsed = urlparse(jobboard_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}"
                # Strip any existing query string to use as the clean search API endpoint
                search_api = f"{base_url}{parsed.path}"
                logger.info(f"  Job board URL: {jobboard_url}")
                logger.info(f"  Search API:    {search_api}")
                logger.info(f"  Base URL:      {base_url}")

                # Step 3: Look up Tulsa city ID and default On-site office location
                cursor.execute("SELECT id FROM cities WHERE city_name = 'Tulsa'")
                result = cursor.fetchone()
                tulsa_city_id = result['id'] if result else None
                logger.info(f"  Tulsa city_id: {tulsa_city_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None

                # Step 4: Establish session
                logger.info("Step 4: Establishing session...")
                if not self.establish_session(jobboard_url):
                    raise Exception("Failed to establish session")

                # Step 5: Fetch job listings from API
                logger.info("Step 5: Fetching job listings from iCIMS API...")
                all_jobs = self.get_job_listings(search_api, base_url)
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} jobs from API")

                # Secondary Tulsa-metro filter: the API location query may return
                # broader Oklahoma results; keep only jobs in cities we serve.
                filtered = [
                    j for j in all_jobs
                    if self._is_tulsa_location(
                        j.get('joblocation') or j.get('location') or ''
                    )
                ]
                logger.info(f"  After Tulsa metro filter: {len(filtered)} of {len(all_jobs)} jobs")
                all_jobs = filtered

                stats['found'] = len(all_jobs)

                # Step 6: Process each job
                for i, job in enumerate(all_jobs):
                    try:
                        title = job.get('jobtitle') or job.get('title') or 'Unknown'
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        job_url = self._build_job_url(job, base_url)
                        if not job_url:
                            logger.warning("  Could not build job URL, skipping")
                            stats['skipped'] += 1
                            continue

                        logger.info(f"  Job URL: {job_url}")

                        existing_id = check_existing_job_by_url(cursor, job_url)
                        if existing_id:
                            stats['updated'] += 1
                            continue

                        description, extracted = self.download_job_details(cursor, job_url)
                        if not description or len(description.strip()) < 100:
                            logger.warning("  Failed to get meaningful job content, skipping")
                            stats['skipped'] += 1
                            continue

                        raw_date = (
                            job.get('posteddatetime')
                            or job.get('datePosted')
                            or job.get('postedDate')
                            or ''
                        )
                        raw_job_type = (
                            extracted.get('time_type')
                            or job.get('jobtype')
                            or job.get('employmentType')
                            or ''
                        )

                        job_data = {
                            'job_title':          title,
                            'job_description':    description,
                            'posting_url':        job_url,
                            'posting_id':         extracted.get('posting_id'),
                            'date_posted':        normalize_date_string(raw_date),
                            'scraping_hash':      self.create_scraping_hash(title, job_url, description),
                            'function':           _map_job_to_function(cursor, title),
                            'job_type_id':        _map_job_type(cursor, raw_job_type),
                            'office_location_id': (
                                extracted.get('office_location_id') or onsite_office_id
                            ),
                            'city_id':            tulsa_city_id,
                            'minimum_salary':     extracted.get('minimum_salary'),
                            'maximum_salary':     extracted.get('maximum_salary'),
                        }

                        job_id = store_job_listing(
                            cursor, job_data, company_id,
                            self.company_config['source_job_board']
                        )
                        logger.info(f"  ✓ Stored job ID: {job_id}")
                        stats['added'] += 1

                        if MAX_JOBS_ADDED and stats['added'] >= MAX_JOBS_ADDED:
                            logger.info(f"  Reached MAX_JOBS_ADDED={MAX_JOBS_ADDED}, stopping early")
                            break

                        time.sleep(0.5)

                    except Exception as e:
                        error_msg = f"Error processing '{job.get('jobtitle') or job.get('title', 'Unknown')}': {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                # Step 7: Mark stale jobs as closed
                logger.info("Step 7: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 8: Update company scrape completion
                logger.info("Step 8: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 9: Log results
                logger.info("Step 9: Logging results...")
                _log_scraping_activity(
                    cursor, self.company_config['source_job_board'], company_id, stats
                )

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        if self.selenium_scraper:
            self.selenium_scraper.cleanup()


def main():
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = SodexoScraper(conn)

        logger.info("Starting Sodexo job scraping...")
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

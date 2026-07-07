#!/usr/bin/env python3
"""
aaa-workday-api-selenium-scrape.py
AAA Club Alliance Workday API + Selenium scraper (Gen 2)

Jobboard URL and company_id are resolved from the DB via get_company_config_by_name.
The Workday API endpoint and base URL are derived from the jobboard URL at runtime.

AAA Club Alliance runs all its regional clubs (including the Tulsa/Oklahoma area)
on a single Workday tenant ("aaamidatlantic"). The jobboard URL stored in
company.jobboard is expected to carry a `locationHierarchy1` facet in its query
string that scopes results to the relevant region — that facet key is extracted
and passed to the API alongside the standard `locations` facet. Because a
location-hierarchy facet can still span a wide area, every job is additionally
validated against TulsaJobSpot's served-city list before being stored; jobs
outside that area are skipped rather than trusted to the facet alone.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_company_config_by_name
from utils.date_utilities import parse_relative_date
from utils.location_utilities import find_served_city, get_city_id
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from urllib.parse import urlparse, parse_qs
import time
import hashlib
import re
from bs4 import BeautifulSoup, NavigableString, Tag
from typing import Dict, List, Optional, Tuple
import requests

logger = setup_logging('AAA Club Alliance')

COMPANY_NAME = 'AAA Club Alliance'
SOURCE_JOB_BOARD = 'AAA Workday Scraper'

# AAA Club Alliance roles: roadside assistance/insurance/membership services,
# travel, retail, plus standard corporate support functions.
_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'tech', 'data',
        'database', 'system', 'network', 'security', 'devops', 'cloud',
        'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile',
    ],
    'Customer Support': [
        'customer service', 'support', 'help desk', 'call center', 'client',
        'member services', 'roadside', 'claims', 'insurance', 'member', 'representative',
    ],
    'Sales': [
        'sales', 'account manager', 'business development', 'bd', 'revenue',
        'membership', 'agent', 'travel consultant', 'travel counselor',
    ],
    'Accounting': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'benefits'],
    'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
    'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
    'Operations': [
        'operations', 'ops', 'supply chain', 'logistics', 'process', 'facility',
        'project manager', 'program manager', 'scrum master', 'project coordinator',
    ],
    'Administrative': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
    'Quality': ['quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'],
    'Security': ['security', 'safety', 'guard', 'protection'],
}

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
        logger.info(f"  Mapped '{job_title}' to function: Other (no match)")
        return result['id']
    return None


def _map_job_type(cursor, time_type: str) -> Optional[int]:
    canonical = normalize_job_type(time_type)
    if not canonical:
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    return result['id'] if result else None


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


def _derive_workday_urls(jobboard_url: str) -> Dict[str, str]:
    """Derive API endpoint and base URL from the public-facing jobboard URL.

    Pattern:
      jobboard:    https://<tenant>.wd<n>.myworkdayjobs.com/<BoardName>?locationHierarchy1=xxx
      api:         https://<tenant>.wd<n>.myworkdayjobs.com/wday/cxs/<tenant>/<BoardName>/jobs
    """
    parsed = urlparse(jobboard_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    tenant = parsed.netloc.split('.')[0]
    path_parts = [p for p in parsed.path.split('/') if p and p.lower() != 'en-us']
    board_name = path_parts[-1] if path_parts else 'External'
    return {
        'origin': origin,
        'api_endpoint': f"{origin}/wday/cxs/{tenant}/{board_name}/jobs",
        'workday_base_url': f"{origin}/{board_name}",
    }


def _extract_facets_from_url(jobboard_url: str) -> Dict:
    """Parse appliedFacets from the jobboard URL query string.

    AAA's board filters by `locationHierarchy1` (repeated for multiple region
    nodes) and `locationRegionStateProvince` rather than the more common
    `locations` facet — all are checked here. parse_qs already collects
    repeated query keys (e.g. multiple `locationHierarchy1` values) into a
    list, which is the shape the Workday API expects for a facet value.
    """
    parsed = urlparse(jobboard_url)
    params = parse_qs(parsed.query)
    facets = {}
    for key in (
        'locations', 'locationHierarchy1', 'locationRegionStateProvince',
        'jobFamilyGroup', 'workerSubType', 'timeType',
    ):
        if key in params:
            facets[key] = params[key]
    return facets


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
            self.driver.get(job_url)
            wait = WebDriverWait(self.driver, timeout)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                return ""
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     '[data-automation-id="jobPostingDescription"], [data-automation-id="jobDescription"]')
                ))
                time.sleep(0.5)
            except TimeoutException:
                time.sleep(1.5)
            return self.driver.page_source
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return self.driver.page_source if self.driver else ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class AAAScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
        })

    def get_job_listings(self, api_endpoint: str, jobboard_url: str, origin: str, facets: Dict) -> List[Dict]:
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None

        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                body = {"limit": limit, "offset": offset, "searchText": ""}
                if facets:
                    body["appliedFacets"] = facets
                    logger.info(f"  Applied facets: {facets}")
                response = self.session.post(
                    api_endpoint,
                    json=body,
                    headers={
                        'Referer': jobboard_url,
                        'Origin': origin,
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                    },
                )
                response.raise_for_status()
                data = response.json()

                if 'jobPostings' not in data:
                    break

                if total_results is None:
                    total_results = data.get('total', 0)
                    logger.info(f"Total jobs available: {total_results}")

                batch = data['jobPostings']
                all_jobs.extend(batch)
                logger.info(f"Retrieved {len(batch)} jobs. Total so far: {len(all_jobs)}")

                offset += limit
                if offset >= total_results or not batch:
                    break
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error fetching jobs at offset {offset}: {e}")
                break

        return all_jobs

    def _map_remote_type_to_office_location(self, cursor, remote_type: str) -> Optional[int]:
        canonical = normalize_work_location(remote_type)
        if not canonical:
            return None
        cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
        result = cursor.fetchone()
        return result['id'] if result else None

    def extract_job_content(self, cursor, html_content: str) -> Tuple[str, Dict]:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            extracted = {
                'posting_id': None, 'time_type': None, 'office_location_id': None,
                'date_closed': None, 'minimum_salary': None, 'maximum_salary': None,
            }
            salary_found = False

            for element in soup.find_all(string=re.compile(r'^R\d{5,}$')):
                extracted['posting_id'] = element.strip()
                break

            for dt in soup.find_all('dt'):
                label = dt.get_text(strip=True).lower()
                dd = dt.find_next_sibling('dd')
                if not dd:
                    continue
                value = dd.get_text(strip=True)

                if re.search(r'remote\s+type', label):
                    extracted['office_location_id'] = self._map_remote_type_to_office_location(cursor, value)
                elif re.search(r'time\s+type', label):
                    extracted['time_type'] = value
                elif re.search(r'salary|pay\s+range|compensation', label):
                    min_sal, max_sal = _parse_salary_from_text(value)
                    if min_sal:
                        extracted['minimum_salary'] = min_sal
                        extracted['maximum_salary'] = max_sal
                        salary_found = True

            if not salary_found:
                min_sal, max_sal = _parse_salary_from_text(soup.get_text())
                if min_sal:
                    extracted['minimum_salary'] = min_sal
                    extracted['maximum_salary'] = max_sal

            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            description = ""
            for selector in [
                '[data-automation-id="jobPostingDescription"]',
                '[data-automation-id="jobDescription"]',
                '.jobPostingDescription',
                '[role="main"]',
                'main',
            ]:
                content = soup.select_one(selector)
                if content and len(content.get_text(strip=True)) > 100:
                    description = _clean_html_description(content)
                    break

            if not description:
                body = soup.find('body')
                if body:
                    description = _clean_html_description(body)

            return description, extracted

        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return "", {}

    def _served_city_for_job(self, job: Dict) -> Optional[str]:
        """Return the matching served city name for a job, or None if it's outside the area."""
        city_name = find_served_city(job.get('locationsText', ''))
        if city_name:
            return city_name
        for loc in job.get('locations', []):
            loc_text = loc if isinstance(loc, str) else loc.get('descriptor', '')
            city_name = find_served_city(loc_text)
            if city_name:
                return city_name
        return None

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company from DB
                logger.info("Step 1: Resolving company from DB...")
                company_config = get_company_config_by_name(cursor, COMPANY_NAME)
                if not company_config:
                    raise ValueError(f"Company '{COMPANY_NAME}' not found in database")
                company_id = company_config['id']
                jobboard_url = company_config['jobboard']
                logger.info(f"  Company ID: {company_id}, Board: {jobboard_url}")

                # Step 2: Derive Workday API URLs from the jobboard URL
                logger.info("Step 2: Deriving Workday API URLs...")
                urls = _derive_workday_urls(jobboard_url)
                logger.info(f"  API endpoint: {urls['api_endpoint']}")

                # Step 3: Establish session
                logger.info("Step 3: Establishing session...")
                self.session.get(jobboard_url)

                # Step 4: Look up On-site office location (city_id resolved per-job below)
                cursor.execute("SELECT id FROM officelocations WHERE LOWER(name) = LOWER('On-Site')")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None

                # Step 5: Fetch jobs from API using facets from the jobboard URL
                logger.info("Step 5: Fetching jobs from API...")
                facets = _extract_facets_from_url(jobboard_url)
                logger.info(f"  Facets from URL: {facets}")
                all_jobs = self.get_job_listings(
                    urls['api_endpoint'], jobboard_url, urls['origin'], facets
                )
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} total jobs")
                stats['found'] = len(all_jobs)

                # Step 6: Process each job, keeping only those in a served city
                logger.info("Step 6: Filtering to served cities and processing jobs...")
                for i, job in enumerate(all_jobs):
                    try:
                        title = job.get('title', 'Unknown')
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        city_name = self._served_city_for_job(job)
                        if not city_name:
                            logger.info(f"  Location '{job.get('locationsText', '')}' not in served area, skipping")
                            stats['skipped'] += 1
                            continue

                        city_id = get_city_id(cursor, city_name)
                        if not city_id:
                            logger.warning(f"  Served city '{city_name}' not found in DB, skipping")
                            stats['skipped'] += 1
                            continue

                        external_path = job.get('externalPath', '')
                        if not external_path:
                            logger.warning("  No externalPath, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = f"{urls['workday_base_url']}{external_path}"

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        html = self.selenium_scraper.get_job_content(job_url)
                        if not html or len(html.strip()) < 100:
                            logger.warning("  Failed to get job page, skipping")
                            stats['skipped'] += 1
                            continue

                        description, extracted = self.extract_job_content(cursor, html)
                        if not description or len(description.strip()) < 100:
                            logger.warning("  Insufficient description, skipping")
                            stats['skipped'] += 1
                            continue

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': job_url,
                            'date_posted': parse_relative_date(job.get('postedOn', '')),
                            'scraping_hash': hashlib.md5(
                                f"{title}{job_url}{description}".encode('utf-8')
                            ).hexdigest(),
                            'function': _map_job_to_function(cursor, title),
                            'job_type_id': _map_job_type(cursor, extracted.get('time_type', '')),
                            'city_id': city_id,
                            'posting_id': extracted.get('posting_id'),
                            'date_closed': extracted.get('date_closed'),
                            'minimum_salary': extracted.get('minimum_salary'),
                            'maximum_salary': extracted.get('maximum_salary'),
                            'office_location_id': extracted.get('office_location_id') or onsite_office_id,
                        }

                        job_id = store_job_listing(cursor, job_data, company_id, SOURCE_JOB_BOARD)
                        logger.info(f"  Stored job ID: {job_id} (city: {city_name})")
                        stats['added'] += 1
                        time.sleep(0.5)

                    except Exception as e:
                        error_msg = f"Error processing '{job.get('title', 'Unknown')}': {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                # Step 7: Mark stale jobs closed
                logger.info("Step 7: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 8: Update company scrape completion
                logger.info("Step 8: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 9: Log results
                logger.info("Step 9: Logging results...")
                _log_scraping_activity(cursor, company_id, stats)

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
        scraper = AAAScraper(conn)

        logger.info(f"Starting {COMPANY_NAME} Workday scraping...")
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

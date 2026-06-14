#!/usr/bin/env python3
"""
oneok-workday-api-scrape-selenium.py
ONEOK Workday API + Selenium scraper (Gen 2)

Fetches all jobs and filters by locationsText for Tulsa, Broken Arrow, or Remote.
Remote jobs are assigned city_id=Tulsa and office_location=Remote.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.date_utilities import parse_relative_date
from utils.location_utilities import match_location_to_city_id, get_city_id, find_served_city
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location

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
from typing import Dict, List, Optional, Tuple
import requests

logger = setup_logging('ONEOK')

# Maps substrings found in Workday "Job Family" / "Category" labels to function names
_CATEGORY_MAPPINGS = {
    'environmental':      'Environmental',
    'information technology': 'Information Technology',
    'engineering':        'Engineering, Mechanical',
    'finance':            'Finance',
    'human resources':    'Human Resources',
    'legal':              'Legal',
    'operations':         'Project Management',
    'maintenance':        'Skilled Labor',
    'safety':             'Security',
    'customer':           'Customer Service',
    'administrative':     'Administration',
}

# Keyword fallback for when no Job Family label is present on the page
_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'data',
        'analyst', 'database', 'system', 'network', 'devops', 'cloud',
        'application', 'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber'
    ],
    'Engineering, Mechanical': [
        'mechanical', 'mech eng', 'mechanical engineer', 'pipeline', 'compressor',
        'facilities', 'plant engineer', 'process engineer'
    ],
    'Engineering, Electrical': [
        'electrical', 'elec eng', 'electrical engineer', 'instrumentation', 'controls'
    ],
    'Engineering, Civil': ['civil', 'civil engineer'],
    'Project Management': [
        'operations', 'ops', 'plant', 'facility', 'gas plant', 'processing',
        'project manager', 'program manager', 'scrum master', 'project coordinator'
    ],
    'Skilled Labor': [
        'technician', 'maintenance', 'mechanic', 'welder', 'electrician',
        'apprentice', 'journeyman', 'crew', 'field', 'operator', 'control room'
    ],
    'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefit', 'compensation'],
    'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue'],
    'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
    'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 'regulatory'],
    'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client'],
    'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
    'Quality': ['quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'],
    'Security': ['security', 'safety', 'guard', 'protection'],
}

# Salary range patterns for regex fallback (when dt/dd doesn't include salary)
_SALARY_PATTERNS = [
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:USD|per\s+year|annually|/year)', False),
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:/hour|per\s+hour|hourly)',        True),
    (r'Salary\s+Range:?\s*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',                    False),
]


def _clean_html_description(element) -> str:
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

    html = serialize(element)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


def _parse_salary_from_text(page_text: str) -> Tuple[Optional[float], Optional[float]]:
    """Regex fallback: scan page text for a salary range."""
    for pattern, is_hourly in _SALARY_PATTERNS:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            try:
                min_sal = float(match.group(1).replace(',', ''))
                max_sal = float(match.group(2).replace(',', ''))
                if is_hourly:
                    min_sal *= 2080
                    max_sal *= 2080
                logger.info(f"  Salary from regex: {match.group(0)}")
                return min_sal, max_sal
            except ValueError:
                continue
    return None, None


def _map_category_to_function(cursor, category: str) -> Optional[int]:
    """Map a Workday Job Family / category string to a function ID."""
    if not category:
        return None
    cursor.execute("SELECT id FROM functions WHERE name = %s", (category,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped category '{category}' (exact match)")
        return result['id']
    category_lower = category.lower()
    for key, function_name in _CATEGORY_MAPPINGS.items():
        if key in category_lower:
            cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped category '{category}' -> '{function_name}'")
                return result['id']
    logger.warning(f"  Could not map category '{category}' to any function")
    return None


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    """Keyword fallback for function mapping when no category label is available."""
    job_title_lower = job_title.lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in job_title_lower:
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


def _map_job_type(cursor, time_type: str) -> Optional[int]:
    canonical = normalize_job_type(time_type)
    if not canonical:
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped time type '{time_type}' -> '{canonical}'")
        return result['id']
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
        'completed'
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
            logger.info("  Loading job page with Selenium...")
            self.driver.get(job_url)
            wait = WebDriverWait(self.driver, timeout)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning("  Body tag not found within timeout")
                return ""
            time.sleep(1.5)
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
        except TimeoutException:
            logger.warning("  Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class OneOKScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        self.company_config = {
            'name': 'ONEOK',
            'website': 'https://www.oneok.com',
            'jobboard': 'https://oneok.wd1.myworkdayjobs.com/ONEOK/',
            'api_endpoint': 'https://oneok.wd1.myworkdayjobs.com/wday/cxs/oneok/ONEOK/jobs',
            'workday_base_url': 'https://oneok.wd1.myworkdayjobs.com/ONEOK',
            'workday_origin': 'https://oneok.wd1.myworkdayjobs.com',
            'company_type_name': 'Public Company',
            'source_job_board': 'ONEOK Workday',
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })

    def establish_session(self) -> bool:
        try:
            logger.info("Establishing session with ONEOK careers page...")
            response = self.session.get(self.company_config['jobboard'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False

    def get_job_listings(self) -> List[Dict]:
        """Fetch all ONEOK jobs (no API-level location filter)."""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None

        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                body = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}

                response = self.session.post(
                    self.company_config['api_endpoint'],
                    json=body,
                    headers={
                        'Referer': self.company_config['jobboard'],
                        'Origin': self.company_config['workday_origin'],
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    }
                )
                response.raise_for_status()
                data = response.json()

                if 'jobPostings' not in data:
                    break

                if total_results is None:
                    total_results = data.get('total', 0)
                    logger.info(f"Total jobs available: {total_results}")

                batch_jobs = data['jobPostings']
                all_jobs.extend(batch_jobs)
                logger.info(f"Retrieved {len(batch_jobs)} jobs. Total so far: {len(all_jobs)}")

                offset += limit
                if offset >= total_results or len(batch_jobs) == 0:
                    break

                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error fetching jobs: {e}")
                break

        return all_jobs

    def filter_served_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Keep jobs in a served metro city or explicitly marked Remote."""
        filtered = []
        logger.info(f"Filtering {len(jobs)} total jobs for served locations...")
        for job in jobs:
            location_text = job.get('locationsText', '')
            title = job.get('title', 'Unknown')
            if find_served_city(location_text):
                filtered.append(job)
                logger.debug(f"  Accept (metro): {title} | {location_text}")
            elif 'remote' in location_text.lower():
                filtered.append(job)
                logger.debug(f"  Accept (remote): {title} | {location_text}")
            else:
                logger.debug(f"  Reject: {title} | {location_text}")
        logger.info(f"Filter: {len(filtered)} / {len(jobs)} jobs accepted")
        return filtered

    def _map_remote_type_to_office_location(self, cursor, remote_type: str) -> Optional[int]:
        canonical = normalize_work_location(remote_type)
        if not canonical:
            return None
        cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
        result = cursor.fetchone()
        if result:
            logger.info(f"  Mapped remote type '{remote_type}' -> '{canonical}' (id: {result['id']})")
            return result['id']
        return None

    def extract_job_content(self, cursor, html_content: str) -> tuple:
        """Parse detail page: return (clean_description, extracted_fields dict)."""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            extracted_fields = {
                'posting_id':       None,
                'time_type':        None,
                'office_location_id': None,
                'category':         None,
                'minimum_salary':   None,
                'maximum_salary':   None,
            }

            # Extract posting ID (R-number pattern)
            for element in soup.find_all(string=re.compile(r'^R\d{5,}$')):
                extracted_fields['posting_id'] = element.strip()
                logger.info(f"  Extracted posting ID: {extracted_fields['posting_id']}")
                break

            # Extract metadata from dt/dd pairs
            salary_found_in_dd = False
            for dt in soup.find_all('dt'):
                label = dt.get_text(strip=True).lower()
                dd = dt.find_next_sibling('dd')
                if not dd:
                    continue
                value = dd.get_text(strip=True)

                if re.search(r'remote\s+type', label):
                    office_location_id = self._map_remote_type_to_office_location(cursor, value)
                    if office_location_id:
                        extracted_fields['office_location_id'] = office_location_id
                    logger.info(f"  Remote type (raw): '{value}'")

                elif re.search(r'time\s+type', label):
                    extracted_fields['time_type'] = value
                    logger.info(f"  Time type (raw): '{value}'")

                elif re.search(r'job\s+family|category|department', label):
                    extracted_fields['category'] = value
                    logger.info(f"  Job family/category: '{value}'")

                elif re.search(r'salary|pay\s+range|compensation', label):
                    min_sal, max_sal = _parse_salary_from_text(value)
                    if min_sal:
                        extracted_fields['minimum_salary'] = min_sal
                        extracted_fields['maximum_salary'] = max_sal
                        salary_found_in_dd = True
                        logger.info(f"  Salary from dt/dd: '{value}'")

            # Salary regex fallback if not found in dt/dd
            if not salary_found_in_dd:
                page_text = soup.get_text()
                min_sal, max_sal = _parse_salary_from_text(page_text)
                if min_sal:
                    extracted_fields['minimum_salary'] = min_sal
                    extracted_fields['maximum_salary'] = max_sal

            # Strip non-content tags then extract clean description
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
                    logger.info(f"  Extracted content using selector: {selector}")
                    description = _clean_html_description(content)
                    break

            if not description:
                body = soup.find('body')
                if body:
                    for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                        tag.decompose()
                    description = _clean_html_description(body)

            logger.info(f"  Extracted description: {len(description)} characters")
            return description, extracted_fields

        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return html_content, {}

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Establish session
                logger.info("Step 1: Establishing session...")
                if not self.establish_session():
                    raise Exception("Failed to establish session")

                # Step 2: Resolve company ID
                logger.info("Step 2: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': self.company_config['website'],
                    'jobboard': self.company_config['jobboard'],
                    'company_type_name': self.company_config['company_type_name'],
                })
                logger.info(f"  Resolved company ID: {company_id}")

                # Step 3: Look up Tulsa city ID and Remote office location ID
                tulsa_city_id = get_city_id(cursor, 'Tulsa')
                logger.info(f"  Tulsa city_id: {tulsa_city_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'Remote'")
                result = cursor.fetchone()
                remote_office_id = result['id'] if result else None
                logger.info(f"  Remote office_location_id: {remote_office_id}")

                # Step 4: Fetch all jobs and filter
                logger.info("Step 4: Fetching jobs from API...")
                all_jobs = self.get_job_listings()
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} total jobs")
                stats['found'] = len(all_jobs)

                served_jobs = self.filter_served_jobs(all_jobs)
                stats['skipped'] += len(all_jobs) - len(served_jobs)

                if not served_jobs:
                    logger.warning("No served-area jobs found after filtering")
                    _log_scraping_activity(cursor, self.company_config['source_job_board'], company_id, stats)
                    return stats

                # Step 5: Process each job
                logger.info(f"Step 5: Processing {len(served_jobs)} jobs...")
                for i, job in enumerate(served_jobs):
                    try:
                        title = job.get('title', 'Unknown')
                        locations_text = job.get('locationsText', '')
                        logger.info(f"Processing job {i+1}/{len(served_jobs)}: {title} | {locations_text}")

                        external_path = job.get('externalPath', '')
                        if not external_path:
                            logger.warning("  No externalPath, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = f"{self.company_config['workday_base_url']}{external_path}"

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        # Resolve city and work location
                        city_name, city_id = match_location_to_city_id(cursor, locations_text)
                        is_remote = 'remote' in locations_text.lower()

                        if city_id:
                            logger.info(f"  City: {city_name} (id: {city_id})")
                            override_office_id = None   # let dt/dd decide work location
                        elif is_remote:
                            city_id = tulsa_city_id
                            override_office_id = remote_office_id
                            logger.info(f"  Remote job — city set to Tulsa, work location set to Remote")
                        else:
                            logger.info(f"  Skipping — location not in served area: '{locations_text}'")
                            stats['skipped'] += 1
                            continue

                        # Scrape detail page
                        job_html = self.selenium_scraper.get_job_content(job_url)
                        if not job_html or len(job_html.strip()) < 100:
                            logger.warning("  Failed to get page content, skipping")
                            stats['skipped'] += 1
                            continue

                        job_description, extracted_fields = self.extract_job_content(cursor, job_html)
                        if not job_description or len(job_description.strip()) < 100:
                            logger.warning("  Insufficient description content, skipping")
                            stats['skipped'] += 1
                            continue

                        # Function: category label takes priority over title keywords
                        function_id = _map_category_to_function(cursor, extracted_fields.get('category'))
                        if not function_id:
                            function_id = _map_job_to_function(cursor, title)

                        # Work location: remote override takes priority over dt/dd
                        office_location_id = override_office_id or extracted_fields.get('office_location_id')

                        time_type_raw = extracted_fields.get('time_type', '')
                        logger.info(f"  Time type from detail page: '{time_type_raw}'")

                        job_data = {
                            'job_title':          title,
                            'job_description':    job_description,
                            'posting_url':        job_url,
                            'date_posted':        parse_relative_date(job.get('postedOn', '')),
                            'scraping_hash':      self.create_scraping_hash(title, job_url, job_description),
                            'function':           function_id,
                            'job_type_id':        _map_job_type(cursor, time_type_raw),
                            'city_id':            city_id,
                            'posting_id':         extracted_fields.get('posting_id'),
                            'office_location_id': office_location_id,
                            'minimum_salary':     extracted_fields.get('minimum_salary'),
                            'maximum_salary':     extracted_fields.get('maximum_salary'),
                        }

                        job_id = store_job_listing(cursor, job_data, company_id,
                                                   self.company_config['source_job_board'])
                        logger.info(f"  Stored job with ID: {job_id}")
                        stats['added'] += 1

                        time.sleep(0.5)

                    except Exception as e:
                        error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
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
                _log_scraping_activity(cursor, self.company_config['source_job_board'], company_id, stats)

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
        scraper = OneOKScraper(conn)

        logger.info("Starting ONEOK job scraping...")
        results = scraper.scrape_jobs()

        logger.info("=== ONEOK SCRAPING SUMMARY ===")
        logger.info(f"Jobs found (API total): {results['found']}")
        logger.info(f"Jobs added:             {results['added']}")
        logger.info(f"Jobs updated:           {results['updated']}")
        logger.info(f"Jobs skipped:           {results['skipped']}")
        logger.info(f"Errors:                 {len(results['errors'])}")

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

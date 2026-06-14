#!/usr/bin/env python3
"""
webco-workday-scrape-selenium.py
Webco Industries Workday API + Selenium scraper (Gen 2)

Uses Workday API location IDs to filter Tulsa-area jobs. Job titles often
include a location suffix (e.g. "Entry Level Manufacturing - Sand Springs")
which is stripped before storing.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.date_utilities import parse_relative_date
from utils.location_utilities import match_location_to_city_id, get_city_id
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
from typing import Dict, List, Optional
import requests

logger = setup_logging('Webco')

# Manufacturing-focused function keyword mappings
_FUNCTION_KEYWORDS = {
    'Machinist': ['machinist', 'cnc', 'lathe', 'mill operator'],
    'Manufacturing': [
        'manufacturing', 'production', 'assembly', 'factory', 'plant operator',
        'entry level manufacturing', 'manufacturing associate', 'production associate'
    ],
    'Engineering, Mechanical': [
        'mechanical engineer', 'mech engineer', 'industrial engineer',
        'process engineer', 'design engineer'
    ],
    'Engineering, Electrical': ['electrical engineer', 'elec engineer'],
    'Engineering, Civil': ['civil engineer'],
    'Skilled Labor': [
        'welder', 'electrician', 'maintenance', 'technician', 'operator',
        'skilled labor', 'trades', 'journeyman', 'apprentice'
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'it ', 'data analyst',
        'database', 'system admin', 'network', 'cyber security', 'devops',
        'cloud', 'web', 'mobile', 'qa tester'
    ],
    'Project Management': [
        'project manager', 'program manager', 'scrum master',
        'project coordinator', 'operations manager'
    ],
    'Quality': ['quality', 'qa', 'qc', 'inspector', 'quality control'],
    'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
    'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue', 'customer'],
    'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
    'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 'regulatory'],
    'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client'],
    'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
    'Security': ['security', 'safety', 'guard', 'protection'],
}


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


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
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
        logger.warning(f"  Could not map time type '{time_type}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped time type '{time_type}' -> '{canonical}'")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
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


class WebcoScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        self.company_config = {
            'name': 'Webco',
            'website': 'https://webcotube.com',
            'jobboard': 'https://webcotube.wd12.myworkdayjobs.com/Webco/',
            'api_endpoint': 'https://webcotube.wd12.myworkdayjobs.com/wday/cxs/webcotube/Webco/jobs',
            'workday_base_url': 'https://webcotube.wd12.myworkdayjobs.com/Webco',
            'workday_origin': 'https://webcotube.wd12.myworkdayjobs.com',
            'tulsa_location_ids': [
                'd0fa73fbb5531007e6bc2740c4f50000',
                'd0fa73fbb5531007e785feed3b5f0000',
                'd0fa73fbb5531007e6bc59d7e24e0000',
                'd0fa73fbb5531007e6bc55a372ce0000',
            ],
            'company_type_name': 'Private Company',
            'source_job_board': 'Webco Workday',
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })

    def clean_job_title(self, raw_title: str) -> str:
        """Strip location suffixes from job titles (e.g. '... - Sand Springs')."""
        if not raw_title:
            return raw_title
        locations = [
            'sand springs', 'tulsa', 'broken arrow', 'oklahoma', 'ok',
            'remote', 'hybrid', 'kellyville', 'mannford'
        ]
        cleaned = raw_title.strip()
        for location in locations:
            cleaned = re.sub(r'\s*-\s*' + re.escape(location) + r'\s*$', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\s*,\s*' + re.escape(location) + r'\s*$', '', cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r'\s*\(\s*' + re.escape(location) + r'\s*\)\s*$', '', cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    def establish_session(self) -> bool:
        try:
            logger.info("Establishing session with Webco careers page...")
            response = self.session.get(self.company_config['jobboard'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False

    def get_job_listings(self) -> List[Dict]:
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None

        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                body = {
                    "appliedFacets": {"locations": self.company_config['tulsa_location_ids']},
                    "limit": limit,
                    "offset": offset,
                    "searchText": ""
                }
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

    def _map_remote_type_to_office_location(self, cursor, remote_type: str) -> Optional[int]:
        canonical = normalize_work_location(remote_type)
        if not canonical:
            logger.warning(f"  Could not map remote type '{remote_type}' to any work location")
            return None
        cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
        result = cursor.fetchone()
        if result:
            logger.info(f"  Mapped remote type '{remote_type}' -> '{canonical}' (id: {result['id']})")
            return result['id']
        logger.warning(f"  Work location '{canonical}' not found in officelocations table")
        return None

    def extract_job_content(self, cursor, html_content: str) -> tuple:
        """Parse detail page: return (clean_description, extracted_fields dict)."""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            extracted_fields = {
                'posting_id': None,
                'time_type': None,
                'office_location_id': None,
            }

            # Extract posting ID (R-number pattern)
            for element in soup.find_all(string=re.compile(r'^R\d{5,}$')):
                extracted_fields['posting_id'] = element.strip()
                logger.info(f"  Extracted posting ID: {extracted_fields['posting_id']}")
                break

            # Extract metadata from dt/dd pairs
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

                # Step 3: Look up fallback city ID (Tulsa) and On-site office location
                tulsa_city_id = get_city_id(cursor, 'Tulsa')
                logger.info(f"  Tulsa fallback city_id: {tulsa_city_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_id = result['id'] if result else None
                logger.info(f"  On-site office_location_id: {onsite_id}")

                # Step 4: Get job listings from API
                logger.info("Step 4: Getting job listings from API...")
                all_jobs = self.get_job_listings()
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} jobs from API")
                stats['found'] = len(all_jobs)

                # Step 5: Process each job
                for i, job in enumerate(all_jobs):
                    try:
                        raw_title = job.get('title', 'Unknown')
                        title = self.clean_job_title(raw_title)
                        if raw_title != title:
                            logger.info(f"  Cleaned title: '{raw_title}' -> '{title}'")
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        # Resolve city: locationsText first, raw title second, Tulsa fallback
                        locations_text = job.get('locationsText', '')
                        city_name, city_id = match_location_to_city_id(cursor, locations_text)
                        if city_id:
                            logger.info(f"  City from locationsText: {city_name} (id: {city_id})")
                        else:
                            city_name, city_id = match_location_to_city_id(cursor, raw_title)
                            if city_id:
                                logger.info(f"  City from title: {city_name} (id: {city_id})")
                            else:
                                city_id = tulsa_city_id
                                logger.info(f"  City: Tulsa (fallback, locationsText='{locations_text}')")

                        external_path = job.get('externalPath', '')
                        if not external_path:
                            logger.warning("  No externalPath, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = f"{self.company_config['workday_base_url']}{external_path}"
                        logger.info(f"  Job URL: {job_url}")

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        job_html = self.selenium_scraper.get_job_content(job_url)
                        if not job_html or len(job_html.strip()) < 100:
                            logger.warning("  Failed to get meaningful job content, skipping")
                            stats['skipped'] += 1
                            continue

                        job_description, extracted_fields = self.extract_job_content(cursor, job_html)
                        if not job_description or len(job_description.strip()) < 100:
                            logger.warning("  Insufficient description content, skipping")
                            stats['skipped'] += 1
                            continue

                        time_type_raw = extracted_fields.get('time_type', '')
                        logger.info(f"  Time type from detail page: '{time_type_raw}'")

                        job_data = {
                            'job_title': title,
                            'job_description': job_description,
                            'posting_url': job_url,
                            'date_posted': parse_relative_date(job.get('postedOn', '')),
                            'scraping_hash': self.create_scraping_hash(title, job_url, job_description),
                            'function': _map_job_to_function(cursor, title),
                            'job_type_id': _map_job_type(cursor, time_type_raw),
                            'city_id': city_id,
                            'posting_id': extracted_fields.get('posting_id'),
                            'office_location_id': onsite_id,
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
        scraper = WebcoScraper(conn)

        logger.info("Starting Webco job scraping...")
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

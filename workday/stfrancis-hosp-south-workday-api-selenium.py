#!/usr/bin/env python3
"""
stfrancis-hosp-south-workday-api-selenium.py
Saint Francis Hospital South — Workday API + Selenium scraper (Gen 2)

The Saint Francis Workday instance is shared across multiple hiring companies.
We scope to Hospital South via appliedFacets.hiringCompany.

locationsText from the API is a company site name (e.g. "South Campus"), not a
city. It is matched against companysite.shortname for this company; if not found
a new site row is created for admin review. city_id comes from the matched site
record (NULL for newly created sites until an admin sets it).
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company, get_or_create_company_site
from utils.date_utilities import parse_relative_date
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location
from utils.location_utilities import match_location_to_city_id
from typing import Tuple

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

logger = setup_logging('Saint Francis Hospital South')

_SALARY_PATTERNS = [
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:USD|per\s+year|annually|/year)', False),
    (r'\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)\s*(?:/hour|per\s+hour|hourly)',        True),
    (r'Salary\s+Range:?\s*\$?([\d,]+\.?\d*)\s*-\s*\$?([\d,]+\.?\d*)',                    False),
]


def _parse_salary_from_text(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Scan text for a salary range. Returns (min, max) or (None, None)."""
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
    'Healthcare': [
        'nurse', 'nursing', 'rn', 'lpn', 'cna', 'registered nurse', 'licensed practical',
        'certified nursing', 'nurse practitioner', 'clinical nurse',
        'therapist', 'therapy', 'technologist', 'technician', 'radiology', 'imaging',
        'laboratory', 'lab', 'phlebotomy', 'phlebotomist', 'respiratory', 'surgical',
        'pharmacy', 'pharmacist', 'ultrasound', 'sonographer', 'diagnostic',
        'rehabilitation', 'physical therapy', 'occupational therapy', 'speech',
        'medical assistant', 'paramedic', 'emt', 'sterile processing',
        'physician', 'doctor', 'md', 'do', 'surgeon', 'hospitalist', 'specialist',
        'anesthesiologist', 'radiologist', 'pathologist', 'cardiologist',
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'database',
        'system', 'network', 'security', 'devops', 'cloud', 'application',
        'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber', 'epic', 'ehr', 'emr',
        'informatics', 'it support', 'helpdesk',
    ],
    'Accounting': [
        'finance', 'financial', 'accounting', 'accountant', 'audit', 'billing',
        'revenue cycle', 'coding', 'coder', 'biller', 'reimbursement',
    ],
    'Human Resources': [
        'hr', 'human resources', 'recruiter', 'talent', 'benefits', 'payroll',
        'workforce',
    ],
    'Administrative': [
        'admin', 'administrative', 'coordinator', 'assistant', 'receptionist',
        'scheduler', 'patient access', 'registration', 'medical records',
        'health information', 'director', 'manager', 'supervisor',
    ],
    'Operations': [
        'project manager', 'program manager', 'operations manager',
        'facilities', 'housekeeping', 'environmental services', 'food service',
        'nutrition', 'dietary', 'linen', 'laundry', 'security', 'maintenance',
        'biomedical', 'biomed', 'engineer', 'engineering',
    ],
    'Marketing': [
        'marketing', 'brand', 'communications', 'social media', 'public relations',
    ],
    'Social Services': [
        'social worker', 'social work', 'case manager', 'case management',
        'chaplain', 'spiritual', 'counselor', 'behavioral health',
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'inspector', 'quality engineer', 'infection control',
        'patient safety', 'risk management', 'compliance',
    ],
    'Legal': [
        'legal', 'attorney', 'counsel', 'compliance', 'contract',
    ],
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
    """Map job title to function ID using keyword matching"""
    job_title_lower = job_title.lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in job_title_lower:
                cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                    return result['id']
    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Healthcare',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{job_title}' to function: Healthcare (default)")
        return result['id']
    logger.warning(f"  Could not map '{job_title}' to any function")
    return None


def _map_job_type(cursor, time_type: str) -> Optional[int]:
    """Map Workday time type string to job_type_id"""
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
    """Handles JavaScript-heavy Workday job pages using Selenium."""

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
            try:
                wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, '[data-automation-id="jobPostingDescription"], [data-automation-id="jobDescription"]')
                ))
                time.sleep(0.5)
            except TimeoutException:
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


class SaintFrancisHospSouthScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        # The Saint Francis Workday tenant hosts multiple hiring companies.
        # hiringCompany scopes the API to Saint Francis Hospital South only.
        self.company_config = {
            'name': 'Saint Francis Hospital South',
            'website': 'https://www.saintfrancis.com',
            'jobboard': 'https://saintfrancis.wd115.myworkdayjobs.com/External?hiringCompany=0799604f508e1000cec34d97003e0000',
            'api_endpoint': 'https://saintfrancis.wd115.myworkdayjobs.com/wday/cxs/saintfrancis/External/jobs',
            'workday_base_url': 'https://saintfrancis.wd115.myworkdayjobs.com/en-US/External',
            'workday_origin': 'https://saintfrancis.wd115.myworkdayjobs.com',
            'hiring_company_id': '0799604f508e1000cec34d97003e0000',
            'company_type_name': 'Non-Profit',
            'source_job_board': 'SFHB Workday',
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
            logger.info(f"Establishing session with {self.company_config['name']} careers page...")
            response = self.session.get(self.company_config['jobboard'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False

    def get_job_listings(self) -> List[Dict]:
        """Fetch job listings from the Workday API, scoped to Saint Francis Hospital South."""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None

        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                body = {
                    "appliedFacets": {"hiringCompany": [self.company_config['hiring_company_id']]},
                    "limit": limit,
                    "offset": offset,
                    "searchText": "",
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
        """Parse detail page HTML: extract metadata fields and clean description."""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            extracted_fields = {
                'posting_id':         None,
                'time_type':          None,
                'office_location_id': None,
                'date_posted':        None,
                'date_closed':        None,
                'minimum_salary':     None,
                'maximum_salary':     None,
                'location_text':      None,
            }
            salary_found_in_dd = False

            try:
                for element in soup.find_all(string=re.compile(r'^R\d{5,}$')):
                    extracted_fields['posting_id'] = element.strip()
                    logger.info(f"  Extracted posting ID: {extracted_fields['posting_id']}")
                    break
            except Exception as e:
                logger.warning(f"  Could not extract posting ID: {e}")

            try:
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
                        logger.info(f"  Remote type (raw): '{value}' -> office_location_id: {office_location_id}")

                    elif re.search(r'time\s+type', label):
                        extracted_fields['time_type'] = value
                        logger.info(f"  Time type (raw): '{value}'")

                    elif re.search(r'salary|pay\s+range|compensation', label):
                        min_sal, max_sal = _parse_salary_from_text(value)
                        if min_sal:
                            extracted_fields['minimum_salary'] = min_sal
                            extracted_fields['maximum_salary'] = max_sal
                            salary_found_in_dd = True
                            logger.info(f"  Salary from dt/dd: '{value}'")

            except Exception as e:
                logger.warning(f"  Could not extract detail page metadata: {e}")

            page_text = soup.get_text(separator='\n')

            if not salary_found_in_dd:
                min_sal, max_sal = _parse_salary_from_text(page_text)
                if min_sal:
                    extracted_fields['minimum_salary'] = min_sal
                    extracted_fields['maximum_salary'] = max_sal

            # Extract "Location: <text>" from description body.
            # May be a city/state string or a site name; caller will try both.
            try:
                loc_match = re.search(r'Location\s*:\s*([^\n<]+)', page_text)
                if loc_match:
                    extracted_fields['location_text'] = loc_match.group(1).strip()
                    logger.info(f"  Location text: '{extracted_fields['location_text']}'")
            except Exception as e:
                logger.warning(f"  Could not extract location text: {e}")

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
                logger.info("Step 1: Establishing session...")
                if not self.establish_session():
                    raise Exception("Failed to establish session")

                logger.info("Step 2: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': self.company_config['website'],
                    'jobboard': self.company_config['jobboard'],
                    'company_type_name': self.company_config['company_type_name'],
                })
                logger.info(f"  Resolved company ID: {company_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None
                logger.info(f"  On-site office_location_id: {onsite_office_id}")

                logger.info("Step 4: Getting job listings from API...")
                all_jobs = self.get_job_listings()
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} jobs from API")
                stats['found'] = len(all_jobs)

                logger.info(f"Step 5: Processing {len(all_jobs)} jobs...")
                for i, job in enumerate(all_jobs):
                    try:
                        title = job.get('title', 'Unknown')
                        api_site_name = (job.get('locationsText') or '').strip()
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title} | site: '{api_site_name}'")

                        external_path = job.get('externalPath', '')
                        if not external_path:
                            logger.warning("  No externalPath found, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = f"{self.company_config['workday_base_url']}{external_path}"
                        logger.info(f"  Job URL: {job_url}")

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            logger.info(f"  Existing job (ID: {existing_job_id}) — timestamps updated")
                            stats['updated'] += 1
                            continue

                        logger.info("  New job — loading detail page...")
                        job_description, extracted_fields = self.extract_job_content(
                            cursor, self.selenium_scraper.get_job_content(job_url)
                        )
                        if not job_description or len(job_description.strip()) < 100:
                            logger.warning("  Failed to get meaningful job content, skipping")
                            stats['skipped'] += 1
                            continue

                        # City resolution — two independent sources, never conflated with site name.
                        # 1. Try the "Location:" text extracted from the description body.
                        desc_location = extracted_fields.get('location_text', '')
                        city_name, city_id = match_location_to_city_id(cursor, desc_location) if desc_location else (None, None)
                        if city_id:
                            logger.info(f"  City from description '{desc_location}': {city_name} (city_id: {city_id})")
                        else:
                            logger.info(f"  No city match from description location: '{desc_location}'")

                        # Site resolution — uses api_site_name (locationsText) exclusively as
                        # shortname. Never falls back to the city string from the description,
                        # which would create or fail to match sites under wrong names.
                        if api_site_name:
                            logger.info(f"  Looking up companysite: company_id={company_id} shortname='{api_site_name}'")
                            cursor.execute(
                                "SELECT id, city_id FROM companysite WHERE company_id = %s AND LOWER(site_name) = LOWER(%s)",
                                (company_id, api_site_name)
                            )
                            site_row = cursor.fetchone()
                            if site_row:
                                logger.info(f"  Found existing site '{api_site_name}' (site city_id: {site_row['city_id']})")
                                if not city_id and site_row['city_id']:
                                    city_id = site_row['city_id']
                                    logger.info(f"  Using city_id {city_id} from companysite record")
                            else:
                                logger.info(f"  No existing site found for '{api_site_name}' under company_id={company_id} — creating")
                                try:
                                    get_or_create_company_site(cursor, company_id, api_site_name, city_id=city_id, logger=logger)
                                except Exception as site_err:
                                    logger.warning(f"  Could not create site '{api_site_name}': {site_err}")
                        else:
                            logger.warning("  locationsText is empty — no site record created or looked up")

                        job_data = {
                            'job_title': title,
                            'job_description': job_description,
                            'posting_url': job_url,
                            'date_posted': parse_relative_date(job.get('postedOn', '')),
                            'scraping_hash': self.create_scraping_hash(title, job_url, job_description),
                            'function': _map_job_to_function(cursor, title),
                            'job_type_id': _map_job_type(cursor, extracted_fields.get('time_type', '')),
                            'city_id': city_id,
                            'posting_id': extracted_fields.get('posting_id'),
                            'date_closed': extracted_fields.get('date_closed'),
                            'minimum_salary': extracted_fields.get('minimum_salary'),
                            'maximum_salary': extracted_fields.get('maximum_salary'),
                            'office_location_id': extracted_fields.get('office_location_id') or onsite_office_id,
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

                logger.info("Step 6: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                logger.info("Step 7: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

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
        scraper = SaintFrancisHospSouthScraper(conn)

        logger.info(f"Starting {scraper.company_config['name']} job scraping...")
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

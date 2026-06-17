#!/usr/bin/env python3
"""
sfh-glenpool-workday-api-selenium.py
Saint Francis Glenpool — Workday API + Selenium scraper (Gen 2)

Part of the Saint Francis shared Workday tenant (saintfrancis.wd115).
See docs/saint-francis-shared-workday.md for the overall pattern.

The Glenpool jobboard URL uses a keyword search (?q=glenpool) rather than
a hiringCompany facet, so the API call uses searchText instead of
appliedFacets. _load_company_config detects which pattern the stored URL
uses and sets the scraper mode accordingly, so this will also work if a
hiringCompany ID is ever assigned.

Because keyword search can return noise, jobs are filtered against the
served cities list before storing — same pattern as Warren Clinic.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company, get_or_create_company_site
from utils.date_utilities import parse_relative_date
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location
from utils.location_utilities import match_location_to_city_id
from typing import Tuple
from urllib.parse import urlparse, parse_qs

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

logger = setup_logging('Workday SFH Glenpool')

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
    'Nursing': [
        'nurse', 'nursing', 'rn', 'lpn', 'cna', 'registered nurse', 'licensed practical',
        'certified nursing', 'nurse practitioner', 'clinical nurse',
    ],
    'Clinical / Allied Health': [
        'therapist', 'therapy', 'technologist', 'technician', 'lab', 'laboratory',
        'phlebotom', 'radiology', 'imaging', 'ultrasound', 'sonographer', 'x-ray',
        'mri', 'ct scan', 'respiratory', 'pharmacy', 'pharmacist',
        'medical assistant', 'social worker', 'social work', 'case manager',
        'care coordinator', 'physical therapy', 'occupational therapy', 'speech',
    ],
    'Physicians / Medical Staff': [
        'physician', 'doctor', 'md', 'do', 'hospitalist', 'specialist',
        'nurse practitioner', 'physician assistant',
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'analyst', 'database',
        'system', 'network', 'security', 'devops', 'cloud', 'application',
        'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber', 'epic', 'ehr', 'emr',
        'informatics', 'it support', 'helpdesk',
    ],
    'Finance': [
        'finance', 'financial', 'accounting', 'accountant', 'audit', 'billing',
        'revenue cycle', 'coding', 'coder', 'biller', 'reimbursement',
    ],
    'Human Resources': [
        'hr', 'human resources', 'recruiter', 'talent', 'benefits', 'payroll',
    ],
    'Administration': [
        'admin', 'administrative', 'coordinator', 'assistant', 'receptionist',
        'scheduler', 'patient access', 'registration', 'medical records',
        'health information', 'director', 'manager', 'supervisor', 'front desk',
    ],
    'Project Management': [
        'project manager', 'program manager', 'operations manager',
    ],
    'Marketing': [
        'marketing', 'brand', 'communications', 'social media', 'public relations',
    ],
    'Facilities / Support Services': [
        'facilities', 'housekeeping', 'environmental services', 'food service',
        'nutrition', 'dietary', 'security', 'maintenance', 'biomedical', 'biomed',
    ],
    'Quality': [
        'quality', 'infection control', 'patient safety', 'risk management', 'compliance',
    ],
    'Legal': [
        'legal', 'attorney', 'counsel', 'compliance', 'contract',
    ],
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
    canonical = normalize_job_type(time_type)
    if not canonical:
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


class SeleniumJobScraper:

    def __init__(self, headless=True):
        self.driver = None
        self.setup_driver(headless)

    def setup_driver(self, headless):
        try:
            chrome_options = SeleniumConfig.get_chrome_options(headless=headless)
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
                    (By.CSS_SELECTOR, '[data-automation-id="jobPostingDescription"], [data-automation-id="jobDescription"]')
                ))
                time.sleep(0.5)
            except TimeoutException:
                time.sleep(1.5)
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass


class GlenpoolScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        # Static identifiers only. All other config (jobboard URL, API mode,
        # search params, company_type_name, Workday URLs) is resolved from
        # the company table at scrape time via _load_company_config().
        self.company_config = {
            'name': 'Saint Francis Glenpool',
            'source_job_board': 'Workday SFH Glenpool',
            # Resolved at runtime:
            'jobboard': None,
            'api_endpoint': None,
            'workday_base_url': None,
            'workday_origin': None,
            'company_type_name': None,
            # One of 'facet' or 'search' — determined from jobboard URL:
            'api_mode': None,
            'hiring_company_id': None,   # used when api_mode == 'facet'
            'search_text': None,         # used when api_mode == 'search'
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })

    def _load_company_config(self, cursor):
        """Resolve all runtime config from the company table record.

        Supports two jobboard URL patterns:
          - hiringCompany facet:  ?hiringCompany=<id>  → api_mode = 'facet'
          - keyword search:       ?q=<term>            → api_mode = 'search'
        """
        cursor.execute("""
            SELECT c.jobboard, ct.name AS company_type_name
            FROM company c
            JOIN company_type ct ON c.company_type = ct.id
            WHERE c.common_name = %s
        """, (self.company_config['name'],))
        row = cursor.fetchone()
        if not row:
            raise Exception(
                f"Company '{self.company_config['name']}' not found in company table."
            )
        if not row['jobboard']:
            raise Exception(
                f"Company '{self.company_config['name']}' has no jobboard URL in the company table."
            )

        jobboard = row['jobboard']
        parsed = urlparse(jobboard)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        tenant = parsed.netloc.split('.')[0]
        qs = parse_qs(parsed.query)

        hiring_ids = qs.get('hiringCompany', [])
        search_terms = qs.get('q', [])

        if hiring_ids:
            api_mode = 'facet'
            self.company_config['hiring_company_id'] = hiring_ids[0]
            logger.info(f"  API mode: facet (hiringCompany={hiring_ids[0]})")
        elif search_terms:
            api_mode = 'search'
            self.company_config['search_text'] = search_terms[0]
            logger.info(f"  API mode: search (q={search_terms[0]})")
        else:
            raise Exception(
                f"Jobboard URL must contain either 'hiringCompany' or 'q' parameter: {jobboard}"
            )

        self.company_config.update({
            'jobboard': jobboard,
            'company_type_name': row['company_type_name'],
            'api_mode': api_mode,
            'workday_origin': origin,
            'workday_base_url': f"{origin}/en-US/External",
            'api_endpoint': f"{origin}/wday/cxs/{tenant}/External/jobs",
        })
        logger.info(f"  Resolved jobboard:      {jobboard}")
        logger.info(f"  Resolved company type:  {row['company_type_name']}")

    def establish_session(self) -> bool:
        try:
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

                if self.company_config['api_mode'] == 'facet':
                    body = {
                        "appliedFacets": {"hiringCompany": [self.company_config['hiring_company_id']]},
                        "limit": limit,
                        "offset": offset,
                        "searchText": "",
                    }
                else:
                    body = {
                        "searchText": self.company_config['search_text'],
                        "limit": limit,
                        "offset": offset,
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
            return None
        cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
        result = cursor.fetchone()
        if result:
            logger.info(f"  Mapped remote type '{remote_type}' -> '{canonical}'")
            return result['id']
        return None

    def extract_job_content(self, cursor, html_content: str) -> tuple:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            extracted_fields = {
                'posting_id': None, 'time_type': None, 'office_location_id': None,
                'date_posted': None, 'date_closed': None,
                'minimum_salary': None, 'maximum_salary': None, 'location_text': None,
            }
            salary_found_in_dd = False

            try:
                for element in soup.find_all(string=re.compile(r'^R\d{5,}$')):
                    extracted_fields['posting_id'] = element.strip()
                    break
            except Exception:
                pass

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
                    elif re.search(r'time\s+type', label):
                        extracted_fields['time_type'] = value
                    elif re.search(r'salary|pay\s+range|compensation', label):
                        min_sal, max_sal = _parse_salary_from_text(value)
                        if min_sal:
                            extracted_fields['minimum_salary'] = min_sal
                            extracted_fields['maximum_salary'] = max_sal
                            salary_found_in_dd = True
            except Exception as e:
                logger.warning(f"  Could not extract detail page metadata: {e}")

            page_text = soup.get_text(separator='\n')

            if not salary_found_in_dd:
                min_sal, max_sal = _parse_salary_from_text(page_text)
                if min_sal:
                    extracted_fields['minimum_salary'] = min_sal
                    extracted_fields['maximum_salary'] = max_sal

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
                '.jobPostingDescription', '[role="main"]', 'main',
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
                logger.info("Step 1: Resolving company config from DB...")
                self._load_company_config(cursor)

                logger.info("Step 2: Establishing session...")
                if not self.establish_session():
                    raise Exception("Failed to establish session")

                logger.info("Step 3: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': None,
                    'jobboard': self.company_config['jobboard'],
                    'company_type_name': self.company_config['company_type_name'],
                })
                logger.info(f"  Resolved company ID: {company_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None

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

                        # City check — keyword search may return jobs outside served cities.
                        # Only store jobs confirmed to be in a served city.
                        desc_location = extracted_fields.get('location_text', '')
                        city_name, city_id = match_location_to_city_id(cursor, desc_location) if desc_location else (None, None)

                        if not city_id:
                            if desc_location:
                                logger.info(f"  Skipping — '{desc_location}' is not in served cities")
                            else:
                                logger.info("  Skipping — no Location: text found in description")
                            stats['skipped'] += 1
                            continue

                        logger.info(f"  City: {city_name} (city_id: {city_id})")

                        # Site resolution — api_site_name (locationsText) as site_name.
                        if api_site_name:
                            cursor.execute(
                                "SELECT id, city_id FROM companysite WHERE company_id = %s AND LOWER(site_name) = LOWER(%s)",
                                (company_id, api_site_name)
                            )
                            site_row = cursor.fetchone()
                            if site_row:
                                logger.info(f"  Found existing site '{api_site_name}' (site city_id: {site_row['city_id']})")
                            else:
                                logger.info(f"  No existing site for '{api_site_name}' — creating")
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
        scraper = GlenpoolScraper(conn)

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

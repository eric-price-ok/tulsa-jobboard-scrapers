#!/usr/bin/env python3
"""
williams-workday-api-selenium-scrape.py
Williams Companies Workday API + Selenium scraper (Gen 2)

Williams does not expose Tulsa location IDs for API filtering, so this scraper
uses a two-stage approach:
  Stage 1: filter API listings by locationsText ('tulsa' or multi-location 'locations')
  Stage 2: validate the detail page div[data-automation-id="locations"] contains 'Tulsa'
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.date_utilities import parse_relative_date
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

logger = setup_logging('Williams')

# Oil & gas midstream function keyword mappings
_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'data',
        'analyst', 'database', 'system', 'network', 'devops', 'cloud',
        'application', 'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber'
    ],
    'Engineering, Mechanical': [
        'mechanical', 'mech eng', 'mechanical engineer', 'pipeline', 'compressor',
        'facilities', 'plant engineer', 'process engineer', 'compression',
        'gas processing', 'midstream', 'facility engineer', 'rotating equipment',
        'turbine', 'pump', 'valve'
    ],
    'Engineering, Electrical': [
        'electrical', 'elec eng', 'electrical engineer', 'instrumentation', 'controls',
        'scada', 'automation', 'control systems', 'plc', 'dcs'
    ],
    'Engineering, Civil': [
        'civil', 'civil engineer', 'structural', 'geotechnical'
    ],
    'Engineering, Other': [
        'chemical engineer', 'petroleum engineer', 'process engineer',
        'reliability engineer', 'corrosion engineer'
    ],
    'Project Management': [
        'operations', 'ops', 'plant', 'facility', 'gas plant', 'processing',
        'dispatch', 'pipeline operations', 'gas transportation', 'midstream operations',
        'project manager', 'program manager', 'scrum master', 'project coordinator'
    ],
    'Skilled Labor': [
        'technician', 'maintenance', 'mechanic', 'welder', 'electrician',
        'apprentice', 'journeyman', 'crew', 'field', 'pipeline technician',
        'compression technician', 'field operations', 'operator', 'control room'
    ],
    'Transportation/Logistics': [
        'pipeline operations', 'gas transportation', 'logistics', 'supply chain',
        'transportation', 'shipping', 'distribution'
    ],
    'Finance': [
        'finance', 'financial', 'accounting', 'accountant', 'treasury',
        'controller', 'audit', 'tax', 'budget'
    ],
    'Human Resources': [
        'hr', 'human resources', 'recruiter', 'talent', 'people',
        'benefit', 'compensation', 'payroll'
    ],
    'Sales': [
        'sales', 'account manager', 'business development', 'bd',
        'revenue', 'customer', 'commercial'
    ],
    'Marketing': [
        'marketing', 'brand', 'digital marketing', 'content',
        'social media', 'communications', 'public relations'
    ],
    'Legal': [
        'legal', 'attorney', 'lawyer', 'counsel', 'compliance',
        'contract', 'regulatory', 'paralegal'
    ],
    'Customer Service': [
        'customer service', 'support', 'help desk', 'call center', 'client'
    ],
    'Administration': [
        'admin', 'administrative', 'coordinator', 'assistant', 'office', 'clerk'
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'
    ],
    'Security': [
        'security', 'safety', 'guard', 'protection', 'hse', 'health safety'
    ],
    'Purchasing': [
        'purchasing', 'procurement', 'buyer', 'sourcing', 'vendor'
    ],
    'Research': [
        'research', 'r&d', 'development', 'innovation', 'scientist'
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
    """Map job title to function ID using keyword matching."""
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
    """Map Workday time type string to job_type_id."""
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


class WilliamsScraper:
    """Williams scraper — two-stage Tulsa filtering, no API-level location IDs."""

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        self.company_config = {
            'name': 'Williams',
            'website': 'https://www.williams.com',
            'jobboard': 'https://williams.wd5.myworkdayjobs.com/External/',
            'api_endpoint': 'https://williams.wd5.myworkdayjobs.com/wday/cxs/williams/External/jobs',
            'workday_base_url': 'https://williams.wd5.myworkdayjobs.com/External',
            'workday_origin': 'https://williams.wd5.myworkdayjobs.com',
            'company_type_name': 'Public Company',
            'source_job_board': 'Williams Workday',
        }

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })

    def discover_tulsa_location_ids(self) -> None:
        """
        One-time helper: probe the API and print all Oklahoma/Tulsa location facet IDs.
        Run with: python williams-workday-api-selenium-scrape.py --discover-locations
        Copy the IDs into company_config['tulsa_location_ids'] to switch to Strategy A.
        """
        try:
            logger.info("Probing Williams API for location facets...")
            response = self.session.post(
                self.company_config['api_endpoint'],
                json={"limit": 0, "offset": 0, "searchText": ""},
                headers={
                    'Referer': self.company_config['jobboard'],
                    'Origin': self.company_config['workday_origin'],
                    'Content-Type': 'application/json',
                    'Accept': 'application/json'
                }
            )
            response.raise_for_status()
            data = response.json()

            location_facets = next(
                (f for f in data.get('facets', []) if f.get('facetParameter') == 'locations'),
                None
            )
            if not location_facets:
                print("No location facets found in API response")
                return

            all_facets = location_facets.get('facets', [])
            print(f"\nTotal location facets: {len(all_facets)}")
            print("\nOklahoma / Tulsa locations:")
            for facet in all_facets:
                descriptor = facet.get('descriptor', '')
                if 'oklahoma' in descriptor.lower() or 'tulsa' in descriptor.lower():
                    print(f"  ID: {facet['id']}  count: {facet.get('count', '?'):>4}  {descriptor}")

            print("\nAll locations:")
            for facet in sorted(all_facets, key=lambda x: x.get('descriptor', '')):
                print(f"  ID: {facet['id']}  count: {facet.get('count', '?'):>4}  {facet.get('descriptor', '')}")

        except Exception as e:
            logger.error(f"Error probing location facets: {e}")

    def establish_session(self) -> bool:
        try:
            logger.info("Establishing session with Williams careers page...")
            response = self.session.get(self.company_config['jobboard'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False

    def get_job_listings(self) -> List[Dict]:
        """Fetch all Williams jobs (no location filter — filter in code)."""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None

        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                body = {"limit": limit, "offset": offset}

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

    def filter_potential_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Stage 1: accept jobs with 'tulsa' in locationsText or multi-location indicator."""
        filtered = []
        logger.info(f"Stage 1 filter: starting with {len(jobs)} total jobs")

        for job in jobs:
            location_text = job.get('locationsText', '')
            title = job.get('title', 'Unknown')

            if 'tulsa' in location_text.lower():
                filtered.append(job)
                logger.info(f"  Stage 1 ACCEPT (Tulsa): {title} | {location_text}")
            elif 'locations' in location_text.lower():
                filtered.append(job)
                logger.info(f"  Stage 1 ACCEPT (multi-location): {title} | {location_text}")
            else:
                logger.debug(f"  Stage 1 reject: {title} | {location_text}")

        logger.info(f"Stage 1: {len(filtered)} / {len(jobs)} jobs passed")
        return filtered

    def validate_tulsa_job(self, html: str, job_title: str) -> bool:
        """Stage 2: confirm detail page div[data-automation-id='locations'] contains Tulsa."""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            locations_div = soup.find('div', {'data-automation-id': 'locations'})

            if locations_div:
                location_text = locations_div.get_text()
                if 'tulsa' in location_text.lower():
                    logger.info(f"  Stage 2 ACCEPT: Tulsa found in locations div")
                    return True
                logger.info(f"  Stage 2 reject: locations div text = {location_text.strip()!r}")
                return False

            # Fallback: search page text for Tulsa indicators
            page_text = soup.get_text()
            for indicator in ['OK Tulsa', 'Tulsa, OK', 'Tulsa,OK']:
                if indicator.lower() in page_text.lower():
                    logger.info(f"  Stage 2 ACCEPT (fallback): found '{indicator}' in page")
                    return True

            logger.info("  Stage 2 reject: no locations div and no Tulsa in page text")
            return False

        except Exception as e:
            logger.warning(f"  Error in Stage 2 validation: {e}")
            return False

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

            # Strip non-content tags, then extract clean description
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
        stats = {
            'found': 0,
            'stage1_accepted': 0,
            'stage1_rejected': 0,
            'stage2_accepted': 0,
            'stage2_rejected': 0,
            'added': 0,
            'updated': 0,
            'skipped': 0,
            'errors': [],
        }

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

                # Step 3: Look up Tulsa city ID and On-site office location ID
                cursor.execute("SELECT id FROM cities WHERE city_name = 'Tulsa'")
                result = cursor.fetchone()
                tulsa_city_id = result['id'] if result else None
                logger.info(f"  Tulsa city_id: {tulsa_city_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None
                logger.info(f"  On-site office_location_id: {onsite_office_id}")

                # Step 4: Fetch all jobs from API
                logger.info("Step 4: Fetching all jobs from Williams Workday API...")
                all_jobs = self.get_job_listings()
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")
                logger.info(f"  Retrieved {len(all_jobs)} total jobs from API")
                stats['found'] = len(all_jobs)

                # Step 5: Stage 1 filter
                logger.info("Step 5: Stage 1 filter — potential Tulsa jobs...")
                potential_tulsa_jobs = self.filter_potential_tulsa_jobs(all_jobs)
                stats['stage1_accepted'] = len(potential_tulsa_jobs)
                stats['stage1_rejected'] = len(all_jobs) - len(potential_tulsa_jobs)
                stats['skipped'] += stats['stage1_rejected']

                if not potential_tulsa_jobs:
                    logger.warning("No potential Tulsa jobs after Stage 1 filter")
                    _log_scraping_activity(cursor, self.company_config['source_job_board'], company_id, stats)
                    return stats

                # Step 6: Process each candidate
                logger.info(f"Step 6: Processing {len(potential_tulsa_jobs)} candidate jobs...")
                for i, job in enumerate(potential_tulsa_jobs):
                    try:
                        title = job.get('title', 'Unknown')
                        location = job.get('locationsText', 'Unknown')
                        logger.info(f"Processing job {i+1}/{len(potential_tulsa_jobs)}: {title} | {location}")

                        external_path = job.get('externalPath', '')
                        if not external_path:
                            logger.warning("  No externalPath, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = f"{self.company_config['workday_base_url']}{external_path}"
                        logger.info(f"  Job URL: {job_url}")

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            logger.info(f"  Existing job (ID: {existing_job_id}) — timestamps updated")
                            stats['updated'] += 1
                            continue

                        # New job — scrape detail page
                        logger.info("  New job — loading detail page...")
                        job_html = self.selenium_scraper.get_job_content(job_url)
                        if not job_html or len(job_html.strip()) < 100:
                            logger.warning("  Failed to get page content, skipping")
                            stats['skipped'] += 1
                            continue

                        # Stage 2 validation
                        if not self.validate_tulsa_job(job_html, title):
                            logger.info("  Job rejected by Stage 2 filter")
                            stats['stage2_rejected'] += 1
                            stats['skipped'] += 1
                            continue

                        stats['stage2_accepted'] += 1
                        logger.info("  Job confirmed as Tulsa position")

                        # Extract description and metadata
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
                            'city_id': tulsa_city_id,
                            'posting_id': extracted_fields.get('posting_id'),
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

                # Step 7: Mark stale jobs as closed
                logger.info("Step 7: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 8: Update company scrape completion
                logger.info("Step 8: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 9: Log results
                logger.info("Step 9: Logging results...")
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
    import sys
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = WilliamsScraper(conn)

        if '--discover-locations' in sys.argv:
            scraper.establish_session()
            scraper.discover_tulsa_location_ids()
            return 0

        logger.info("Starting Williams job scraping (two-stage Tulsa filter)...")
        results = scraper.scrape_jobs()

        logger.info("=== WILLIAMS SCRAPING SUMMARY ===")
        logger.info(f"Jobs found (API total):   {results['found']}")
        logger.info(f"Stage 1 accepted:         {results['stage1_accepted']}")
        logger.info(f"Stage 1 rejected:         {results['stage1_rejected']}")
        logger.info(f"Stage 2 confirmed Tulsa:  {results['stage2_accepted']}")
        logger.info(f"Stage 2 non-Tulsa:        {results['stage2_rejected']}")
        logger.info(f"Jobs added:               {results['added']}")
        logger.info(f"Jobs updated:             {results['updated']}")
        logger.info(f"Jobs skipped:             {results['skipped']}")
        logger.info(f"Errors:                   {len(results['errors'])}")

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

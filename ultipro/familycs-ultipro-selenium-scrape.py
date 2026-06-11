#!/usr/bin/env python3
"""
familycs-ultipro-selenium-scrape.py
Family & Children's Services UltiPro Job Board Scraper
Selenium-based extraction using data-automation selectors
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time
import hashlib
import re
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import check_existing_job_by_url, store_job_listing, mark_stale_jobs_closed
from utils.company_operations import get_company_config_by_name, get_or_create_company_site
from utils.utility_methods import normalize_job_type
from utils.selenium_config import SeleniumConfig
from utils.date_utilities import normalize_date_string
from utils.location_utilities import find_served_city, get_city_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('familycs_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Social services / non-profit focused function keyword mapping
_FUNCTION_KEYWORDS = {
    'Social Work': [
        'social worker', 'case manager', 'case management', 'family services',
        'child welfare', 'foster care', 'adoption', 'outreach', 'family support'
    ],
    'Counseling': [
        'counselor', 'therapist', 'therapy', 'mental health', 'behavioral health',
        'substance abuse', 'crisis', 'clinical', 'psychologist'
    ],
    'Healthcare': [
        'nurse', 'nursing', 'medical', 'health', 'physician', 'doctor',
        'care coordinator', 'patient'
    ],
    'Education': [
        'teacher', 'educator', 'instructor', 'trainer', 'tutor', 'youth',
        'early childhood', 'childcare'
    ],
    'Information Technology': [
        'software', 'developer', 'data', 'analyst', 'it', 'technology', 'tech',
        'system', 'network', 'database', 'engineer'
    ],
    'Human Resources': [
        'hr', 'human resources', 'recruiter', 'talent', 'payroll', 'benefits'
    ],
    'Finance': [
        'finance', 'financial', 'accounting', 'accountant', 'controller', 'billing', 'accounts'
    ],
    'Administration': [
        'admin', 'administrative', 'coordinator', 'director', 'manager', 'supervisor',
        'executive', 'assistant', 'specialist', 'operations'
    ],
    'Customer Service': [
        'customer service', 'support', 'representative', 'receptionist', 'front desk'
    ],
    'Legal': ['legal', 'attorney', 'compliance', 'contracts'],
    'Marketing': ['marketing', 'communications', 'brand', 'media', 'outreach'],
}


def _map_job_to_function(cursor, job_category: str, job_title: str = '') -> Optional[int]:
    """Map job category (or title fallback) to function ID"""
    search_text = (job_category or job_title or '').lower()
    if not search_text:
        return None

    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in search_text:
                cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"  Mapped '{job_category or job_title}' to function: {function_name}")
                    return result['id']

    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Other',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{job_category or job_title}' to function: Other")
        return result['id']
    return None


def _map_job_type(cursor, schedule: str) -> Optional[int]:
    """Map UltiPro schedule string to job_type_id via normalize_job_type"""
    canonical = normalize_job_type(schedule)
    if not canonical:
        logger.warning(f"  Could not map '{schedule}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{schedule}' to job type: {canonical}")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _map_office_location(cursor, location_type: str) -> Optional[int]:
    """Map UltiPro location type string to office_location_id"""
    if not location_type:
        return None
    location_lower = location_type.lower().replace('-', ' ')

    location_mappings = {
        'remote': ['remote', 'work from home', 'wfh'],
        'hybrid': ['hybrid', 'flexible'],
        'in office': ['onsite', 'on site', 'in office', 'office'],
    }

    for canonical, variations in location_mappings.items():
        if any(var in location_lower for var in variations):
            cursor.execute("SELECT id FROM officelocations WHERE LOWER(name) = %s", (canonical,))
            result = cursor.fetchone()
            if result:
                return result['id']

    cursor.execute("SELECT id FROM officelocations WHERE LOWER(name) = %s", (location_lower,))
    result = cursor.fetchone()
    return result['id'] if result else None


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


class SeleniumJobScraper:
    """Handles UltiPro job pages using Selenium + data-automation selectors"""

    DETAIL_BASE_URL = "https://recruiting.ultipro.com"

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()

    def setup_driver(self):
        """Initialize Chrome WebDriver"""
        try:
            chrome_options = SeleniumConfig.get_chrome_options(self.headless)
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
            SeleniumConfig.setup_driver_timeouts(self.driver)
            logger.info("Chrome WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Load job board and extract all job listings using data-automation selectors"""
        try:
            logger.info(f"Loading job board: {job_board_url}")
            self.driver.get(job_board_url)

            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-automation="opportunity"]')
            ))
            time.sleep(3)

            job_elements = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-automation="opportunity"]'
            )
            logger.info(f"Found {len(job_elements)} job opportunities")

            jobs = []
            for i, job_element in enumerate(job_elements):
                try:
                    job_data = self._extract_job_metadata(job_element, i + 1)
                    if job_data:
                        jobs.append(job_data)
                except Exception as e:
                    logger.error(f"Error extracting job {i + 1}: {e}")

            logger.info(f"Extracted {len(jobs)} job listings")
            return jobs

        except TimeoutException:
            logger.error("Timeout waiting for job listings to load")
            return []
        except Exception as e:
            logger.error(f"Error loading job board: {e}")
            return []

    def _first_text(self, element, selector, by=By.CSS_SELECTOR) -> Optional[str]:
        """Return text of first matching child element, or None without waiting."""
        results = element.find_elements(by, selector)
        return results[0].text.strip() if results else None

    def _extract_job_metadata(self, job_element, job_number: int) -> Optional[Dict]:
        """Extract job metadata from a single UltiPro job element.

        Uses find_elements (plural) for all optional fields so Selenium never
        waits on the implicit-wait timeout for missing elements.
        """
        try:
            title_link = job_element.find_element(
                By.CSS_SELECTOR, '[data-automation="job-title"]'
            )
            job_title = title_link.text.strip()
            href = title_link.get_attribute('href')
            if href.startswith('http'):
                posting_url = href
            elif href.startswith('/'):
                posting_url = f"{self.DETAIL_BASE_URL}{href}"
            else:
                posting_url = f"{self.DETAIL_BASE_URL}/{href}"

            date_raw = self._first_text(job_element, '[data-automation="opportunity-posted-date"]')
            req_raw = self._first_text(
                job_element,
                './/strong[contains(text(), "Requisition Number")]/following-sibling::span',
                by=By.XPATH
            )

            job_data = {
                'job_title': job_title,
                'posting_url': posting_url,
                'date_posted': normalize_date_string(date_raw) if date_raw else None,
                'posting_id': req_raw,
                'schedule': self._first_text(job_element, '[data-automation="job-hours"]'),
                'job_category': self._first_text(job_element, '[data-automation="job-category"]'),
                'location_type': self._first_text(job_element, '[data-automation="job-location-type"]'),
                'physical_location': self._first_text(job_element, '[data-automation="physical-location"]'),
            }

            logger.info(
                f"Job {job_number}: {job_data['job_title']} | "
                f"{job_data.get('physical_location', 'no location')}"
            )
            return job_data

        except Exception as e:
            logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None

    def get_page_html(self, url: str) -> str:
        """Load a page and return raw HTML"""
        try:
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 12)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(1.5)
            return self.driver.page_source
        except TimeoutException:
            return self.driver.page_source if self.driver else ""
        except Exception as e:
            logger.warning(f"  Error loading page: {e}")
            return ""

    def extract_job_description(self, html_content: str) -> str:
        """Extract plain-text job description from detail page HTML"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            body = soup.find('body')
            if not body:
                return ""

            for br in body.find_all('br'):
                br.replace_with('\n')

            description = body.get_text(separator='\n', strip=True)

            copyright_idx = description.lower().find('copyright')
            if copyright_idx != -1:
                description = description[:copyright_idx]

            description = re.sub(r'\n{3,}', '\n\n', description).strip()

            logger.info(f"  Extracted description: {len(description)} chars")
            return description

        except Exception as e:
            logger.warning(f"  Error extracting description: {e}")
            return ""

    def extract_location_description(self, html_content: str) -> Optional[str]:
        """Extract office location description from job detail page"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            location_span = soup.find('span', {'data-automation': 'location-description'})
            if location_span:
                location_text = location_span.get_text(strip=True)
                logger.info(f"  Found location description: '{location_text}'")
                return location_text
            logger.warning("  No location-description span found on detail page")
            return None
        except Exception as e:
            logger.warning(f"  Error extracting location description: {e}")
            return None

    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class FamilyCSUltiProScraper:
    """Family & Children's Services UltiPro job scraper"""

    COMPANY_NAME = "Family & Children's Services"
    MAX_NEW_JOBS = 5  # cap detail-page scrapes per run; existing jobs still get timestamps updated

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company — must already exist in DB (jobboard URL comes from there)
                logger.info("Step 1: Resolving company...")
                company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
                if not company_config:
                    raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
                company_id = company_config['id']
                job_board_url = company_config['jobboard']
                logger.info(f"  Company ID: {company_id}, Board: {job_board_url}")

                # Step 2: Fetch all job listings from UltiPro job board
                logger.info("Step 2: Fetching job listings...")
                all_jobs = self.selenium_scraper.get_job_listings(job_board_url)
                if not all_jobs:
                    raise Exception("No jobs retrieved from job board")

                # Step 3: Filter to served cities
                logger.info("Step 3: Filtering for served cities...")
                local_jobs = [
                    job for job in all_jobs
                    if find_served_city(job.get('physical_location', ''))
                ]
                stats['found'] = len(local_jobs)
                logger.info(
                    f"  {len(local_jobs)} of {len(all_jobs)} jobs are in served cities"
                )

                if not local_jobs:
                    logger.warning("No jobs found in served cities")
                    return stats

                # Step 4: Process each job — update timestamps for existing jobs,
                # scrape detail pages for new ones (capped at MAX_NEW_JOBS per run)
                new_jobs_scraped = 0
                for i, job_metadata in enumerate(local_jobs):
                    try:
                        title = job_metadata.get('job_title', 'Unknown')
                        logger.info(f"Processing job {i+1}/{len(local_jobs)}: {title}")

                        existing_job_id = check_existing_job_by_url(
                            cursor, job_metadata['posting_url']
                        )
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        if new_jobs_scraped >= self.MAX_NEW_JOBS:
                            logger.info(
                                f"Reached new-job limit of {self.MAX_NEW_JOBS}, "
                                f"stopping detail scraping for this run"
                            )
                            break

                        html = self.selenium_scraper.get_page_html(job_metadata['posting_url'])
                        if not html or len(html.strip()) < 100:
                            logger.warning("  Failed to get job page content, skipping")
                            stats['skipped'] += 1
                            continue

                        description = self.selenium_scraper.extract_job_description(html)
                        if not description or len(description.strip()) < 50:
                            logger.warning("  Insufficient description, skipping")
                            stats['skipped'] += 1
                            continue

                        # Detect UltiPro "unsupported browser" or "not found" error pages
                        if 'unsupported browser' in description.lower():
                            logger.warning("  Got UltiPro unsupported-browser error page, skipping")
                            stats['skipped'] += 1
                            continue

                        # Company site from the detail page's location-description element
                        location_description = self.selenium_scraper.extract_location_description(html)
                        company_site_id = None
                        if location_description:
                            company_site_id = get_or_create_company_site(
                                cursor, company_id, location_description
                            )

                        city_name = find_served_city(job_metadata.get('physical_location', ''))
                        city_id = get_city_id(cursor, city_name) if city_name else None

                        job_data = {
                            'job_title': job_metadata['job_title'],
                            'posting_url': job_metadata['posting_url'],
                            'posting_id': job_metadata.get('posting_id'),
                            'job_description': description,
                            'date_posted': job_metadata.get('date_posted'),
                            'source_job_board': "Family & Children's Services UltiPro",
                            'company_site_id': company_site_id,
                            'scraping_hash': hashlib.md5(
                                f"{job_metadata['job_title']}{job_metadata['posting_url']}{description}".encode()
                            ).hexdigest(),
                            'function': _map_job_to_function(
                                cursor,
                                job_metadata.get('job_category', ''),
                                job_metadata['job_title']
                            ),
                            'job_type_id': _map_job_type(cursor, job_metadata.get('schedule', '')),
                            'office_location_id': _map_office_location(
                                cursor, job_metadata.get('location_type', '')
                            ),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, company_id)
                        logger.info(f"  Stored job with ID: {job_id}")
                        stats['added'] += 1
                        new_jobs_scraped += 1
                        time.sleep(1.0)

                    except Exception as e:
                        error_msg = f"Error processing {job_metadata.get('job_title', 'Unknown')}: {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                # Step 5: Mark stale jobs as closed
                logger.info("Step 5: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 6: Update company scrape completion
                logger.info("Step 6: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 7: Log results
                logger.info("Step 7: Logging results...")
                _log_scraping_activity(
                    cursor, "Family & Children's Services UltiPro", company_id, stats
                )

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        """Clean up resources"""
        if self.selenium_scraper:
            self.selenium_scraper.cleanup()


def main():
    """Main execution function"""
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = FamilyCSUltiProScraper(conn)

        logger.info("Starting Family & Children's Services UltiPro job scraping...")
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

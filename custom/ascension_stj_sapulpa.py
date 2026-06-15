#!/usr/bin/env python3
"""
ascension-st-john-selenium-scrape.py
Ascension St. John Sapulpa Job Board Scraper
Handles Ascension job boards with Selenium for full job description extraction
"""

from utils.selenium_config import SeleniumConfig
from utils.posting_operations import store_job_listing, load_active_jobs_cache, check_job_in_cache, update_job_verified_timestamp, mark_stale_jobs_closed
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location
from utils.company_operations import get_company_config_by_name
from utils.db_connection import get_database_connection
from utils.location_utilities import get_city_id
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time
from datetime import datetime
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional

class DatabaseManager:
    """Handles all PostgreSQL database operations"""

    def __init__(self, connection_string: str = None):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}
        self.logger = logging.getLogger(__name__)

    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)

    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache first, then database"""
        job_id = check_job_in_cache(job_url, self.active_jobs_cache)
        if job_id:
            return job_id
        return None

    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing using posting_operations"""
        with self.conn.cursor() as cursor:
            enhanced_job_data = job_data.copy()
            enhanced_job_data['company_id'] = company_id
            return store_job_listing(cursor, enhanced_job_data, company_id, 'Ascension St. John - Sapulpa')

    def _map_job_type(self, job_type: str) -> Optional[int]:
        """Map job type string to job_type_id via canonical name lookup"""
        canonical = normalize_job_type(job_type)
        if not canonical:
            return None
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{job_type}' to job type: {canonical}")
                return result['id']
        self.logger.warning(f"  Job type '{canonical}' not found in database")
        return None

    def _map_job_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords"""
        if not job_title:
            return None

        job_title_lower = job_title.lower()

        with self.conn.cursor() as cursor:
            function_keywords = {
                'Healthcare': [
                    'nurse', 'rn', 'lpn', 'lvn', 'cna', 'physician', 'doctor', 'md', 'do',
                    'therapist', 'technician', 'tech', 'medical', 'clinical', 'healthcare',
                    'pharmacy', 'pharmacist', 'respiratory', 'radiology', 'laboratory', 'paramedic',
                    'surgical', 'surgery', 'anesthesia', 'emergency', 'critical care', 'Cardiologist',
                    'icu', 'er', 'med/surg', 'pediatric', 'oncology', 'cardiology', 'Otolaryngologist'
                ],
                'Administration': [
                    'admin', 'administrative', 'coordinator', 'assistant', 'office',
                    'secretary', 'clerk', 'registration', 'receptionist', 'scheduler'
                ],
                'Information Technology': [
                    'it', 'technology', 'software', 'data', 'systems', 'network',
                    'computer', 'analyst', 'developer', 'programmer'
                ],
                'Finance': ['finance', 'financial', 'accounting', 'accountant', 'billing', 'revenue'],
                'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people'],
                'Operations': ['operations', 'ops', 'supply', 'logistics', 'facility', 'maintenance'],
                'Customer Service': ['customer', 'service', 'support', 'call center', 'patient'],
                'Security': ['security', 'safety', 'guard', 'protection'],
                'Food Service': ['food', 'dietary', 'nutrition', 'kitchen', 'cafeteria', 'cook'],
                'Environmental Services': ['housekeeping', 'environmental', 'cleaning', 'custodial']
            }

            for function_name, keywords in function_keywords.items():
                if any(keyword in job_title_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                        return result['id']

        self.logger.info(f"  No function match for '{job_title}'")
        return None

    def _map_office_location(self, office_location: str) -> Optional[int]:
        """Map office location string to office_location_id via canonical name lookup"""
        canonical = normalize_work_location(office_location)
        if not canonical:
            return None
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{office_location}' to office location: {canonical}")
                return result['id']
        self.logger.warning(f"  Office location '{canonical}' not found in database")
        return None

    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE company
                SET last_full_scrape_completed = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")

    def log_scraping_activity(self, job_board: str, stats: Dict):
        """Log scraping results"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO scrapinglog (
                    job_board, jobs_found, jobs_added, jobs_updated,
                    jobs_skipped, errors, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                job_board,
                stats.get('found', 0),
                stats.get('added', 0),
                stats.get('updated', 0),
                stats.get('skipped', 0),
                str(stats.get('errors', [])),
                'completed'
            ))

class SeleniumJobScraper:
    """Handles JavaScript-heavy job pages using Selenium"""

    def __init__(self, headless=True, logger=None):
        self.driver = None
        self.headless = headless
        self.logger = logger or logging.getLogger(__name__)
        self.setup_driver()

    def setup_driver(self):
        """Initialize Chrome WebDriver with optimized options"""
        try:
            chrome_options = SeleniumConfig.get_chrome_options(self.headless)

            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)

            SeleniumConfig.setup_driver_timeouts(self.driver)

            self.logger.info("Optimized Selenium WebDriver initialized")

        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            self.logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)

            wait = WebDriverWait(self.driver, timeout)

            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                self.logger.warning(f"  Body tag not found within timeout")
                return ""

            time.sleep(1.5)

            page_source = self.driver.page_source
            self.logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source

        except TimeoutException:
            self.logger.warning(f"  Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""

        except Exception as e:
            self.logger.error(f"  Error loading job page: {e}")
            return ""

    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Load job board and extract all job listings"""
        try:
            self.logger.info(f"Loading job board: {job_board_url}")
            self.driver.get(job_board_url)

            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'ul[data-ph-at-id="jobs-list"]')))

            time.sleep(3)

            job_elements = self.driver.find_elements(By.CSS_SELECTOR, 'li.jobs-list-item')
            self.logger.info(f"Found {len(job_elements)} job opportunities")

            jobs = []
            for i, job_element in enumerate(job_elements):
                try:
                    job_data = self.extract_job_metadata(job_element, i + 1)
                    if job_data:
                        jobs.append(job_data)
                except Exception as e:
                    self.logger.error(f"Error extracting job {i + 1}: {e}")

            self.logger.info(f"Successfully extracted {len(jobs)} job listings")
            return jobs

        except TimeoutException:
            self.logger.error("Timeout waiting for job listings to load")
            return []
        except Exception as e:
            self.logger.error(f"Error loading job board: {e}")
            return []

    def extract_job_metadata(self, job_element, job_number: int) -> Dict:
        """Extract job metadata from a single job element"""
        job_data = {}

        try:
            try:
                title_span = job_element.find_element(By.CSS_SELECTOR, 'span[data-ps*="span-"]')
                job_data['job_title'] = title_span.text.strip()
            except NoSuchElementException:
                self.logger.warning(f"  Job {job_number}: Could not find job title")
                return None

            try:
                link_element = job_element.find_element(By.CSS_SELECTOR, '[data-ph-at-id="job-link"]')
                job_data['posting_url'] = link_element.get_attribute('href')
            except NoSuchElementException:
                self.logger.warning(f"  Job {job_number}: Could not find job URL")
                return None

            try:
                posting_id_span = job_element.find_element(By.XPATH, './/span[string-length(text()) > 0 and string-length(text()) < 10 and translate(text(), "0123456789", "") = ""]')
                job_data['posting_id'] = posting_id_span.text.strip()
            except NoSuchElementException:
                self.logger.warning(f"  Job {job_number}: Could not find posting ID")
                job_data['posting_id'] = None

            job_data['job_type'] = self._find_value_by_label(job_element, "Job Type")
            job_data['office_location'] = self._find_value_by_label(job_element, "Commute Type")

            shift_info = self._extract_shift_info(job_element)
            job_data.update(shift_info)

            job_data['date_posted'] = datetime.now().date()

            self.logger.info(f"Job {job_number}: {job_data['job_title']}")
            return job_data

        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None

    def _find_value_by_label(self, container, label_text: str) -> Optional[str]:
        """Find span containing label, then get next span's text"""
        try:
            label_span = container.find_element(By.XPATH, f'.//span[@class="sr-only au-target" and contains(text(), "{label_text}")]')
            value_span = label_span.find_element(By.XPATH, './following-sibling::span[1]')
            value = value_span.text.strip()

            if value:
                self.logger.info(f"    Found {label_text}: {value}")
                return value
            else:
                self.logger.warning(f"    {label_text} span found but empty")
                return None

        except NoSuchElementException:
            self.logger.warning(f"    Could not find {label_text}")
            return None

    def _extract_shift_info(self, container) -> Dict:
        """Extract shift information and return boolean flags"""
        shift_data = {
            'first_shift': True,
            'second_shift': False,
            'third_shift': False
        }

        try:
            shift_span = container.find_element(By.CSS_SELECTOR, 'span.psShift, span[class*="psShift"]')
            shift_text = shift_span.text.strip().lower()

            if shift_text:
                self.logger.info(f"    Found shift info: {shift_text}")

                if 'night' in shift_text or '11p' in shift_text or 'midnight' in shift_text:
                    shift_data = {'first_shift': False, 'second_shift': False, 'third_shift': True}
                elif 'evening' in shift_text or '3p' in shift_text or 'afternoon' in shift_text:
                    shift_data = {'first_shift': False, 'second_shift': True, 'third_shift': False}
                elif 'day' in shift_text or '7a' in shift_text or 'morning' in shift_text:
                    shift_data = {'first_shift': True, 'second_shift': False, 'third_shift': False}

        except NoSuchElementException:
            self.logger.warning(f"    Could not find shift information, defaulting to first shift")

        return shift_data

    def extract_job_description(self, html_content: str) -> str:
        """Extract job description from HTML content"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()

                body_text = body.get_text(separator=' ', strip=True)
                if len(body_text) > 100:
                    self.logger.info(f"  Extracted job description: {len(body_text)} characters")
                    return body_text

            self.logger.warning(f"  No meaningful job description found")
            return html_content

        except Exception as e:
            self.logger.warning(f"Error extracting job description: {e}")
            return html_content

    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class AscensionStJohnScraper:
    """Ascension St. John Sapulpa job scraper"""
    COMPANY_NAME = 'Ascension St. John - Sapulpa'

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        self.company_id = self.company_config['id']
        self.logger = setup_logging(self.company_config['name'])
        self.db.logger = self.logger
        self.selenium_scraper = SeleniumJobScraper(headless=True, logger=self.logger)

    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {
            'found': 0,
            'added': 0,
            'updated': 0,
            'skipped': 0,
            'errors': []
        }

        try:
            self.logger.info("Step 1: Using company from database")

            self.logger.info("Step 1.5: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            # Look up city_id once
            with self.db.conn.cursor() as cursor:
                city_id = get_city_id(cursor, 'Sapulpa')
            if not city_id:
                raise Exception("Could not find city_id for 'Sapulpa'")

            self.logger.info("Step 2: Getting job listings from Ascension job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")

            stats['found'] = len(job_listings)
            self.logger.info(f"✓ Found {len(job_listings)} jobs")

            for i, job_metadata in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")

                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue

                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        self.logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue

                    job_description = self.selenium_scraper.extract_job_description(job_html)

                    job_type_id = self.db._map_job_type(job_metadata.get('job_type', ''))
                    function_id = self.db._map_job_function(job_metadata.get('job_title', ''))
                    office_location_id = self.db._map_office_location(job_metadata.get('office_location', ''))

                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': job_metadata['posting_id'],
                        'job_description': job_description,
                        'date_posted': job_metadata['date_posted'],
                        'job_type_id': job_type_id,
                        'function': function_id,
                        'office_location_id': office_location_id,
                        'city_id': city_id,
                        'first_shift': job_metadata['first_shift'],
                        'second_shift': job_metadata['second_shift'],
                        'third_shift': job_metadata['third_shift'],
                        'minimum_salary': None,
                        'maximum_salary': None,
                        'pay_frequency': None,
                        'scraping_hash': None
                    }

                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ✓ Stored job with ID: {job_id}")
                    stats['added'] += 1

                    time.sleep(1.0)

                except Exception as e:
                    error_msg = f"Error processing job {job_metadata.get('job_title', 'Unknown')}: {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1

            self.logger.info("Step 4: Marking stale jobs as closed...")
            with self.db.conn.cursor() as cursor:
                mark_stale_jobs_closed(cursor, self.company_id, self.logger)

            self.logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)

            self.logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('Ascension St. John - Sapulpa', stats)

        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            self.logger.error(error_msg)
            stats['errors'].append(error_msg)

        return stats

    def cleanup(self):
        """Clean up resources"""
        if self.selenium_scraper:
            self.selenium_scraper.cleanup()

def main():
    """Main execution function"""
    scraper = None
    try:
        db_manager = DatabaseManager()
        scraper = AscensionStJohnScraper(db_manager)

        scraper.logger.info("Starting Ascension St. John Sapulpa job scraping...")
        results = scraper.scrape_jobs()

        scraper.logger.info("=== SCRAPING SUMMARY ===")
        scraper.logger.info(f"Jobs found: {results['found']}")
        scraper.logger.info(f"Jobs added: {results['added']}")
        scraper.logger.info(f"Jobs updated: {results['updated']}")
        scraper.logger.info(f"Jobs skipped: {results['skipped']}")
        scraper.logger.info(f"Errors: {len(results['errors'])}")

        if results['errors']:
            scraper.logger.error("Errors encountered:")
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

    return 0

if __name__ == "__main__":
    exit(main())

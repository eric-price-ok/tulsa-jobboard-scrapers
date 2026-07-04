#!/usr/bin/env python3
"""
quiktrip.py
QuikTrip custom job board scraper (Gen 2)

QuikTrip's search page returns jobs across many cities (not just the searched
one), so every row's jobLocation is matched against the served-cities table
and non-served jobs are skipped. The results list also appears to load a
fixed batch (25) with no visible next/load-more control, so the scraper
scrolls repeatedly and stops once the row count stops growing, to also
tolerate the page turning out to be infinite-scroll after all.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import (
    store_job_listing, load_active_jobs_cache, check_job_in_cache,
    update_job_verified_timestamp, mark_stale_jobs_closed,
)
from utils.company_operations import get_company_config_by_name
from utils.date_utilities import normalize_date_string
from utils.location_utilities import find_served_city, get_city_id
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from urllib.parse import urljoin
import time
import hashlib
import re
from bs4 import BeautifulSoup
from typing import Dict, List, Optional

logger = setup_logging('QuikTrip')

COMPANY_NAME = 'QuikTrip'
SOURCE_JOB_BOARD = 'QT Custom Scraper'

# QuikTrip is primarily convenience-store retail, but also staffs corporate
# office functions (IT, accounting, etc.), warehouses, and its own trucking fleet.
_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'analyst', 'database',
        'system', 'network', 'security engineer', 'devops', 'cloud',
        'application', 'help desk', 'it support', 'cyber',
    ],
    'Transportation/Logistics': [
        'driver', 'cdl', 'truck', 'transport', 'logistics', 'fleet', 'dispatcher',
    ],
    'Supply Chain': [
        'warehouse', 'distribution', 'supply chain', 'inventory', 'shipping',
        'receiving', 'procurement', 'purchasing',
    ],
    'Food Service': [
        'kitchen', 'cook', 'food service', 'chef', 'culinary',
    ],
    'Skilled Labor': [
        'maintenance', 'technician', 'mechanic', 'electrician', 'facilities',
        'construction', 'equipment operator',
    ],
    'Finance': [
        'finance', 'financial', 'accounting', 'accountant', 'audit', 'payroll',
        'treasury',
    ],
    'Human Resources': [
        'human resources', 'hr', 'recruiter', 'talent', 'benefits',
    ],
    'Legal': [
        'legal', 'attorney', 'counsel', 'compliance', 'real estate',
    ],
    'Marketing': [
        'marketing', 'brand', 'communications', 'social media', 'advertising',
    ],
    'Project Management': [
        'project manager', 'program manager', 'store manager', 'operations manager',
        'district manager', 'division manager',
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'food safety',
    ],
    'Security': [
        'security', 'safety', 'loss prevention', 'asset protection',
    ],
    'Administration': [
        'admin', 'administrative', 'coordinator', 'assistant', 'clerk', 'office',
    ],
    'Customer Service': [
        'customer service', 'cashier', 'clerk', 'assistant manager', 'team leader',
        'shift leader', 'attendant',
    ],
}


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    title_lower = (job_title or '').lower()
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


class SeleniumJobScraper:

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()

    def setup_driver(self):
        try:
            chrome_options = SeleniumConfig.get_chrome_options(self.headless)
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except Exception:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            SeleniumConfig.setup_driver_timeouts(self.driver)
            logger.info("Selenium WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def get_job_listings(self, jobboard_url: str) -> List[Dict]:
        """Load the search page and collect every data-row, scrolling until the
        row count stops growing (handles both a static 25-result page and a
        possible infinite-scroll page without knowing which it is)."""
        try:
            logger.info(f"Loading job board: {jobboard_url}")
            self.driver.get(jobboard_url)

            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.contentQuikTrip')))
            time.sleep(1.5)

            last_count = -1
            stable_rounds = 0
            max_rounds = 20
            for _ in range(max_rounds):
                rows = self.driver.find_elements(By.CSS_SELECTOR, 'div.contentQuikTrip .data-row')
                current_count = len(rows)
                if current_count == last_count:
                    stable_rounds += 1
                    if stable_rounds >= 2:
                        break
                else:
                    stable_rounds = 0
                last_count = current_count
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.5)

            row_elements = self.driver.find_elements(By.CSS_SELECTOR, 'div.contentQuikTrip .data-row')
            logger.info(f"Found {len(row_elements)} job rows")
            if len(row_elements) == 25:
                logger.warning("Exactly 25 rows found — verify this isn't a page-size cap on the live site")

            jobs = []
            for i, row in enumerate(row_elements):
                try:
                    job_data = self._extract_row_metadata(row, i + 1)
                    if job_data:
                        jobs.append(job_data)
                except Exception as e:
                    logger.error(f"Error extracting row {i + 1}: {e}")

            logger.info(f"Successfully extracted {len(jobs)} job listings")
            return jobs

        except TimeoutException:
            logger.error("Timeout waiting for job listings to load")
            return []
        except Exception as e:
            logger.error(f"Error loading job board: {e}")
            return []

    def _extract_row_metadata(self, row, row_number: int) -> Optional[Dict]:
        try:
            title_link = row.find_element(By.CSS_SELECTOR, '.jobdetail-phone.visible-phone .jobTitle-link')
            job_title = title_link.text.strip()
            href = title_link.get_attribute('href')
            posting_url = urljoin('https://careers.quiktrip.com/', href) if href else None

            try:
                date_element = row.find_element(By.CSS_SELECTOR, '.jobDate.visible-phone')
                date_raw = date_element.text.strip()
            except NoSuchElementException:
                date_raw = None

            try:
                location_element = row.find_element(By.CSS_SELECTOR, '.jobLocation')
                location_raw = location_element.text.strip()
            except NoSuchElementException:
                location_raw = ''

            # find_served_city lowercases both sides, so "TULSA, OK, US, 74116"
            # matches the served city name regardless of casing.
            city_name = find_served_city(location_raw)

            logger.info(f"Row {row_number}: {job_title} - {location_raw} - {date_raw}")
            return {
                'job_title': job_title,
                'posting_url': posting_url,
                'date_posted_raw': date_raw,
                'date_posted': normalize_date_string(date_raw) if date_raw else None,
                'location_raw': location_raw,
                'city_name': city_name,
            }
        except Exception as e:
            logger.error(f"Error extracting metadata for row {row_number}: {e}")
            return None

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
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.jobDisplay')))
            except TimeoutException:
                pass
            time.sleep(1.0)
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return self.driver.page_source if self.driver else ""

    def extract_job_description(self, html_content: str) -> str:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            container = soup.select_one('div.jobDisplay')
            if container:
                text = re.sub(r'\s+', ' ', container.get_text(separator=' ', strip=True)).strip()
                if len(text) > 50:
                    logger.info(f"  Extracted description via div.jobDisplay: {len(text)} characters")
                    return text[:50000]

            # Generic fallback if jobDisplay isn't present for some reason
            for selector in ['main', 'article', 'div[role="main"]']:
                content = soup.select_one(selector)
                if content:
                    text = re.sub(r'\s+', ' ', content.get_text(separator=' ', strip=True)).strip()
                    if len(text) > 100:
                        logger.info(f"  Extracted description via fallback '{selector}': {len(text)} characters")
                        return text[:50000]

            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                    tag.decompose()
                text = re.sub(r'\s+', ' ', body.get_text(separator=' ', strip=True)).strip()
                if len(text) > 100:
                    logger.info(f"  Extracted description from body fallback: {len(text)} characters")
                    return text[:50000]

            logger.warning("  No meaningful job description found")
            return ""
        except Exception as e:
            logger.warning(f"Error extracting job description: {e}")
            return ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class QuikTripScraper:

    def __init__(self, conn):
        self.conn = conn
        with self.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{COMPANY_NAME}' not found in database")
        self.company_id = self.company_config['id']
        self.selenium_scraper = SeleniumJobScraper(headless=True)

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                logger.info("Step 1: Loading active jobs cache...")
                active_jobs_cache = load_active_jobs_cache(cursor, self.company_id)

                logger.info("Step 2: Getting job listings from QuikTrip search page...")
                all_jobs = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
                if not all_jobs:
                    raise Exception("No jobs retrieved from job board")
                stats['found'] = len(all_jobs)
                logger.info(f"  Retrieved {len(all_jobs)} total rows")

                logger.info("Step 3: Filtering to served cities and processing jobs...")
                for i, job in enumerate(all_jobs):
                    try:
                        title = job.get('job_title', 'Unknown')
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        if not job.get('posting_url'):
                            logger.warning("  No posting URL found, skipping")
                            stats['skipped'] += 1
                            continue

                        if not job.get('city_name'):
                            logger.info(f"  Location '{job.get('location_raw', '')}' not in served area, skipping")
                            stats['skipped'] += 1
                            continue

                        city_id = get_city_id(cursor, job['city_name'])
                        if not city_id:
                            logger.warning(f"  Served city '{job['city_name']}' not found in DB, skipping")
                            stats['skipped'] += 1
                            continue

                        existing_job_id = check_job_in_cache(job['posting_url'], active_jobs_cache)
                        if existing_job_id:
                            update_job_verified_timestamp(cursor, existing_job_id)
                            stats['updated'] += 1
                            continue

                        html = self.selenium_scraper.get_job_content(job['posting_url'])
                        description = self.selenium_scraper.extract_job_description(html) if html else ""
                        if not description or len(description.strip()) < 50:
                            logger.warning("  Failed to get meaningful job content, skipping")
                            stats['skipped'] += 1
                            continue

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': job['posting_url'],
                            'date_posted': job.get('date_posted'),
                            'scraping_hash': self.create_scraping_hash(title, job['posting_url'], description),
                            'function': _map_job_to_function(cursor, title),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, self.company_id, SOURCE_JOB_BOARD)
                        logger.info(f"  Stored job ID: {job_id} (city: {job['city_name']})")
                        stats['added'] += 1
                        time.sleep(0.5)

                    except Exception as e:
                        error_msg = f"Error processing '{job.get('job_title', 'Unknown')}': {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                logger.info("Step 4: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, self.company_id, logger)

                logger.info("Step 5: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, self.company_id)

                logger.info("Step 6: Logging results...")
                _log_scraping_activity(cursor, self.company_id, stats)

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
        scraper = QuikTripScraper(conn)

        logger.info(f"Starting {COMPANY_NAME} job scraping...")
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

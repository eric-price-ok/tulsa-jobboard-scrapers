#!/usr/bin/env python3
"""
sagenet-ultipro-scrape.py
Sagenet UltiPro job board scraper (Gen 2)

Job board URL is read from company.jobboard at runtime rather than hardcoded.
Jobs with no matching function keyword default to Information Technology.
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company, get_or_create_company_site
from utils.date_utilities import parse_relative_date
from utils.location_utilities import TULSA_METRO_CITIES
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location

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
from bs4 import BeautifulSoup, NavigableString, Tag
from typing import Dict, List, Optional, Tuple

logger = setup_logging('Sagenet')

FILTER_TO_TULSA = False

_FUNCTION_KEYWORDS = {
    'Information Technology': [
        'software', 'developer', 'programmer', 'data', 'database',
        'system', 'network', 'security', 'devops', 'cloud', 'application',
        'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber', 'engineer',
        'architect', 'infrastructure', 'noc', 'helpdesk', 'help desk',
        'technician', 'analyst', 'it ', 'telecom', 'wireless', 'iot',
    ],
    'Sales': ['sales', 'account manager', 'business development', 'account executive'],
    'Customer Service': ['customer service', 'support', 'client'],
    'Project Management': ['project manager', 'program manager', 'operations manager'],
    'Finance': ['finance', 'financial', 'accounting', 'accountant', 'audit'],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'benefits'],
    'Marketing': ['marketing', 'brand', 'communications'],
    'Legal': ['legal', 'attorney', 'counsel', 'compliance', 'contract'],
    'Administration': ['admin', 'administrative', 'coordinator', 'assistant'],
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
    """Map job title to function ID; defaults to Information Technology."""
    title_lower = job_title.lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in title_lower:
                cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                    return result['id']
    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Information Technology',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{job_title}' to function: Information Technology (default)")
        return result['id']
    logger.warning(f"  Could not map '{job_title}' to any function")
    return None


def _map_job_type(cursor, schedule: str) -> Optional[int]:
    canonical = normalize_job_type(schedule)
    if not canonical:
        logger.warning(f"  Could not map schedule '{schedule}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped schedule '{schedule}' -> '{canonical}'")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _map_work_location(cursor, location_type: str) -> Optional[int]:
    canonical = normalize_work_location(location_type)
    if not canonical:
        logger.warning(f"  Could not map location type '{location_type}' to any work location")
        return None
    cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped location type '{location_type}' -> '{canonical}' (id: {result['id']})")
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
    """Handles Selenium browsing for UltiPro listing and detail pages."""

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

    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Load job board page and return list of raw job metadata dicts."""
        try:
            logger.info(f"Loading job board: {job_board_url}")
            self.driver.get(job_board_url)
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located(
                (By.CSS_SELECTOR, '[data-automation="opportunity"]')
            ))
            time.sleep(2)

            job_elements = self.driver.find_elements(
                By.CSS_SELECTOR, '[data-automation="opportunity"]'
            )
            logger.info(f"Found {len(job_elements)} job cards")

            jobs = []
            for i, element in enumerate(job_elements):
                try:
                    data = self._extract_card_metadata(element, i + 1)
                    if data:
                        jobs.append(data)
                except Exception as e:
                    logger.error(f"Error extracting job card {i + 1}: {e}")

            logger.info(f"Extracted {len(jobs)} job listings")
            return jobs

        except TimeoutException:
            logger.error("Timeout waiting for job listing page to load")
            return []
        except Exception as e:
            logger.error(f"Error loading job board: {e}")
            return []

    def _extract_card_metadata(self, element, job_number: int) -> Optional[Dict]:
        """Extract metadata from a single data-automation="opportunity" card."""
        data = {}

        try:
            title_link = element.find_element(By.CSS_SELECTOR, '[data-automation="job-title"]')
            data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href') or ''
            if href.startswith('http'):
                data['posting_url'] = href
            elif href.startswith('/'):
                data['posting_url'] = f"https://recruiting.ultipro.com{href}"
            else:
                data['posting_url'] = f"https://recruiting.ultipro.com/{href}"
        except NoSuchElementException:
            logger.warning(f"  Card {job_number}: no job-title element, skipping")
            return None

        try:
            el = element.find_element(By.CSS_SELECTOR, '[data-automation="opportunity-posted-date"]')
            data['posted_date_raw'] = el.text.strip()
        except NoSuchElementException:
            data['posted_date_raw'] = None

        try:
            el = element.find_element(
                By.XPATH, './/strong[contains(text(), "Requisition Number")]/following-sibling::span'
            )
            data['requisition_number'] = el.text.strip()
        except NoSuchElementException:
            data['requisition_number'] = None

        try:
            el = element.find_element(By.CSS_SELECTOR, '[data-automation="job-hours"]')
            data['schedule'] = el.text.strip()
        except NoSuchElementException:
            data['schedule'] = None

        try:
            el = element.find_element(By.CSS_SELECTOR, '[data-automation="job-category"]')
            data['job_category'] = el.text.strip()
        except NoSuchElementException:
            data['job_category'] = None

        try:
            el = element.find_element(By.CSS_SELECTOR, '[data-automation="job-location-type"]')
            data['location_type'] = el.text.strip()
        except NoSuchElementException:
            data['location_type'] = None

        try:
            el = element.find_element(By.CSS_SELECTOR, '[data-automation="physical-location"]')
            data['physical_location'] = el.text.strip()
        except NoSuchElementException:
            data['physical_location'] = None

        logger.info(
            f"  Card {job_number}: {data['job_title']} | "
            f"{data.get('physical_location')} | {data.get('posted_date_raw')}"
        )
        return data

    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job detail page and return page HTML."""
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
                    (By.CSS_SELECTOR, '[data-automation="job-description"], .opportunity-description')
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


class SagenetScraper:

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)

        self.company_config = {
            'name': 'Sagenet',
            'website': 'https://www.sagenet.com',
            'company_type_name': 'Private Company',
            'source_job_board': 'Sagenet Ultipro',
        }

    def _is_tulsa_location(self, physical_location: str) -> bool:
        if not physical_location:
            return False
        location_lower = physical_location.lower()
        return any(city.lower() in location_lower for city in TULSA_METRO_CITIES)

    def extract_job_content(self, html_content: str) -> Tuple[str, Dict]:
        """Parse detail page HTML. Returns (description_html, extracted_fields)."""
        extracted: Dict = {
            'location_description': None,
            'requisition_number': None,
        }

        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            loc_span = soup.find(attrs={'data-automation': 'location-description'})
            if loc_span:
                extracted['location_description'] = loc_span.get_text(strip=True)
                logger.info(f"  Location description: '{extracted['location_description']}'")

            for strong in soup.find_all('strong'):
                if 'requisition' in strong.get_text(strip=True).lower():
                    sibling = strong.find_next_sibling()
                    if sibling:
                        extracted['requisition_number'] = sibling.get_text(strip=True)
                        break

            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            description = ""
            for selector in [
                '[data-automation="job-description"]',
                '[data-automation="opportunity-description"]',
                '.opportunity-description',
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

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        return hashlib.md5(f"{title}{url}{description}".encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company ID
                logger.info("Step 1: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': self.company_config['website'],
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
                job_board_url = row['jobboard']
                logger.info(f"  Job board URL: {job_board_url}")

                # Step 3: Look up Tulsa city ID and default On-site office location
                cursor.execute("SELECT id FROM cities WHERE city_name = 'Tulsa'")
                result = cursor.fetchone()
                tulsa_city_id = result['id'] if result else None
                logger.info(f"  Tulsa city_id: {tulsa_city_id}")

                cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
                result = cursor.fetchone()
                onsite_office_id = result['id'] if result else None

                # Step 4: Get job listings via Selenium
                logger.info("Step 4: Getting job listings from UltiPro page...")
                all_jobs = self.selenium_scraper.get_job_listings(job_board_url)
                if not all_jobs:
                    raise Exception("No jobs retrieved from listing page")
                logger.info(f"  Retrieved {len(all_jobs)} job cards")

                # Step 5: Optional Tulsa location filter
                if FILTER_TO_TULSA:
                    filtered = [
                        j for j in all_jobs
                        if self._is_tulsa_location(j.get('physical_location', ''))
                    ]
                    logger.info(f"  After Tulsa filter: {len(filtered)} of {len(all_jobs)} jobs")
                    all_jobs = filtered

                stats['found'] = len(all_jobs)

                # Step 6: Process each job
                for i, job_meta in enumerate(all_jobs):
                    try:
                        title = job_meta.get('job_title', 'Unknown')
                        job_url = job_meta.get('posting_url', '')
                        logger.info(f"Processing job {i+1}/{len(all_jobs)}: {title}")

                        if not job_url:
                            logger.warning("  No posting URL, skipping")
                            stats['skipped'] += 1
                            continue

                        existing_id = check_existing_job_by_url(cursor, job_url)
                        if existing_id:
                            stats['updated'] += 1
                            continue

                        html = self.selenium_scraper.get_job_content(job_url)
                        if not html or len(html.strip()) < 100:
                            logger.warning("  Failed to get detail page content, skipping")
                            stats['skipped'] += 1
                            continue

                        description, extracted = self.extract_job_content(html)
                        if not description or len(description.strip()) < 100:
                            logger.warning("  Description too short, skipping")
                            stats['skipped'] += 1
                            continue

                        company_site_id = None
                        loc_desc = extracted.get('location_description')
                        if loc_desc:
                            company_site_id = get_or_create_company_site(
                                cursor, company_id, loc_desc,
                                city_id=tulsa_city_id, logger=logger
                            )

                        posting_id = (
                            extracted.get('requisition_number')
                            or job_meta.get('requisition_number')
                        )

                        job_data = {
                            'job_title': title,
                            'job_description': description,
                            'posting_url': job_url,
                            'posting_id': posting_id,
                            'date_posted': parse_relative_date(job_meta.get('posted_date_raw', '')),
                            'scraping_hash': self.create_scraping_hash(title, job_url, description),
                            'function': _map_job_to_function(cursor, title),
                            'job_type_id': _map_job_type(cursor, job_meta.get('schedule', '')),
                            'office_location_id': (
                                _map_work_location(cursor, job_meta.get('location_type', ''))
                                or onsite_office_id
                            ),
                            'city_id': tulsa_city_id,
                            'company_site_id': company_site_id,
                        }

                        job_id = store_job_listing(
                            cursor, job_data, company_id,
                            self.company_config['source_job_board']
                        )
                        logger.info(f"  ✓ Stored job ID: {job_id}")
                        stats['added'] += 1

                        time.sleep(0.5)

                    except Exception as e:
                        error_msg = f"Error processing '{job_meta.get('job_title', 'Unknown')}': {e}"
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
        scraper = SagenetScraper(conn)

        logger.info("Starting Sagenet job scraping...")
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

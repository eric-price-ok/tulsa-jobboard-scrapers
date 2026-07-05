#!/usr/bin/env python3
"""
template-applitrack-selenium-attachment.py
Template for Applitrack scrapers where job descriptions are attached as
DOCX or PDF files linked inside an AppliTrackJobPostingAttachments div.

To create a new scraper from this template:
  1. Copy this file: cp template-applitrack-selenium-attachment.py <district>-applitrack-selenium.py
  2. Replace COMPANY_NAME with the exact company.common_name value in the DB
  3. Replace SOURCE_JOB_BOARD with the label for scrapinglog (e.g. 'BAPS Applitrack')
  4. Replace CITY_NAME with the served city name (e.g. 'Broken Arrow'), or None if not served
  5. Rename the scraper class (e.g. BrokenArrowJobScraper)
  6. Update the log file name in basicConfig
  7. Run: python dry_run.py applitrack/<district>-applitrack-selenium.py

Dependencies: mammoth, PyPDF2 (not in requirements.txt — install separately)
"""

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
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import requests
import os
import tempfile

import mammoth
import PyPDF2

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import check_existing_job_by_url, store_job_listing, mark_stale_jobs_closed
from utils.company_operations import get_company_config_by_name
from utils.selenium_config import SeleniumConfig
from utils.date_utilities import normalize_date_string
from utils.location_utilities import get_city_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('district_scraper.log', encoding='utf-8'),  # REPLACE
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Adjust keywords to match the district's typical position types
_FUNCTION_KEYWORDS = {
    'Administrative': [
        'principal', 'assistant principal', 'superintendent', 'director',
        'coordinator', 'supervisor', 'admin', 'leadership', 'administration'
    ],
    'Education': [
        'teacher', 'instructor', 'educator', 'faculty', 'classroom',
        'special education', 'librarian', 'counselor', 'speech',
        'occupational therapist'
    ],
    'Information Technology': [
        'technology', 'computer', 'network', 'systems', 'tech support'
    ],
    'Operations': [
        'bus', 'driver', 'transportation', 'mechanic', 'fleet'
    ],
    'Accounting': [
        'business manager', 'finance clerk', 'accounting', 'bookkeeper', 'payroll'
    ],
    'Security': [
        'security', 'safety', 'sro', 'resource officer', 'guard'
    ],
    'Maintenance': [
        'maintenance', 'custodial', 'janitor', 'groundskeeper', 'facilities'
    ],
}


def _map_job_to_function(cursor, position_type: str) -> Optional[int]:
    """Map Applitrack position_type to function ID"""
    if position_type:
        position_lower = position_type.lower()
        for function_name, keywords in _FUNCTION_KEYWORDS.items():
            for keyword in keywords:
                if keyword in position_lower:
                    cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped '{position_type}' to function: {function_name}")
                        return result['id']

    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Other',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{position_type}' to function: Other")
        return result['id']
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    cursor.execute("""
        UPDATE company
        SET last_full_scrape_completed = CURRENT_TIMESTAMP
        WHERE id = %s
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


class DocumentProcessor:
    """Downloads and extracts text from attached DOCX/PDF job description files"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        })

    def download_and_extract_text(self, document_url: str) -> str:
        try:
            logger.info(f"  Downloading document: {document_url}")
            response = self.session.get(document_url, timeout=30)
            response.raise_for_status()

            content_type = response.headers.get('content-type', '').lower()
            is_pdf = 'pdf' in content_type or document_url.lower().endswith('.pdf')
            is_docx = 'word' in content_type or document_url.lower().endswith('.docx')

            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(response.content)
                temp_path = temp_file.name

            try:
                if is_docx:
                    text = self._extract_docx_text(temp_path)
                elif is_pdf:
                    text = self._extract_pdf_text(temp_path)
                else:
                    try:
                        text = self._extract_docx_text(temp_path)
                    except Exception:
                        text = self._extract_pdf_text(temp_path)

                logger.info(f"  Extracted {len(text)} characters from document")
                return text
            finally:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"  Error processing document {document_url}: {e}")
            return ""

    def _extract_docx_text(self, file_path: str) -> str:
        try:
            with open(file_path, 'rb') as docx_file:
                result = mammoth.extract_raw_text(docx_file)
                return result.value
        except Exception as e:
            logger.warning(f"Failed to extract DOCX text: {e}")
            return ""

    def _extract_pdf_text(self, file_path: str) -> str:
        try:
            text = ""
            with open(file_path, 'rb') as pdf_file:
                pdf_reader = PyPDF2.PdfReader(pdf_file)
                for page in pdf_reader.pages:
                    text += page.extract_text() + "\n"
            return text
        except Exception as e:
            logger.warning(f"Failed to extract PDF text: {e}")
            return ""


class SeleniumJobScraper:
    """Loads the Applitrack job board page and returns HTML"""

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()

    def setup_driver(self):
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

    def get_page_content(self, url: str) -> str:
        try:
            logger.info(f"Loading page: {url}")
            self.driver.get(url)
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(2)
            page_source = self.driver.page_source
            logger.info(f"Retrieved page source: {len(page_source)} characters")
            return page_source
        except TimeoutException:
            logger.warning("Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""
        except Exception as e:
            logger.error(f"Error loading page: {e}")
            return ""

    def cleanup(self):
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except Exception:
                pass


class DistrictJobScraper:  # REPLACE class name
    """Applitrack job scraper — attachment (DOCX/PDF) description variant"""

    COMPANY_NAME = 'REPLACE WITH EXACT company.common_name'
    SOURCE_JOB_BOARD = 'REPLACE WITH SOURCE LABEL'
    CITY_NAME = 'REPLACE WITH CITY OR SET TO None'

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.document_processor = DocumentProcessor()

    def extract_job_id_and_url(self, job_element, base_url: str) -> tuple:
        title2_span = job_element.find('span', class_='title2')
        if title2_span:
            match = re.search(r'JobID\s*:?\s*(\d+)', title2_span.get_text(), re.IGNORECASE)
            if match:
                job_id = match.group(1)
                logger.info(f"  Found JobID: {job_id}")
                job_url = (
                    f"{base_url}&AppliTrackJobId={job_id}"
                    f"&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1"
                )
                return job_id, job_url
        logger.warning("  Could not extract JobID from job element")
        return None, None

    def extract_job_data_from_listing(self, job_element, base_url: str) -> Dict:
        job_data = {}
        try:
            title_td = job_element.find('td', id='wrapword')
            if title_td:
                job_data['job_title'] = title_td.get_text(strip=True)
                logger.info(f"  Title: '{job_data['job_title']}'")
            else:
                logger.warning("  No td with id='wrapword' found")

            job_id, job_url = self.extract_job_id_and_url(job_element, base_url)
            if job_id and job_url:
                job_data['posting_id'] = job_id
                job_data['posting_url'] = job_url

            for li in job_element.find_all('li'):
                label_span = li.find('span', class_='label')
                if not label_span:
                    continue
                normal_span = li.find('span', class_='normal')
                if not normal_span:
                    continue
                label_text = label_span.get_text()
                value_text = normal_span.get_text(strip=True)

                if 'Date Posted' in label_text and 'date_posted' not in job_data:
                    job_data['date_posted'] = normalize_date_string(value_text)
                    logger.info(f"  Date Posted: {job_data['date_posted']}")
                elif 'Position Type' in label_text and 'position_type' not in job_data:
                    job_data['position_type'] = value_text
                    logger.info(f"  Position Type: {value_text}")

        except Exception as e:
            logger.error(f"Error extracting job data: {e}")

        return job_data

    def get_job_description_from_attachment(self, job_element) -> str:
        """Download and extract text from the attached DOCX/PDF in the listing"""
        try:
            attachments_div = job_element.find('div', class_='AppliTrackJobPostingAttachments')
            if not attachments_div:
                logger.warning("  No AppliTrackJobPostingAttachments div found")
                return ""

            for link in attachments_div.find_all('a', href=True):
                href = link.get('href')
                if not href or 'BrowseFile.aspx' not in href:
                    continue

                if href.startswith('/'):
                    document_url = f"https://www.applitrack.com{href}"
                elif not href.startswith('http'):
                    document_url = f"https://www.applitrack.com/baschools/onlineapp/{href}"
                else:
                    document_url = href

                logger.info(f"  Found attachment: {document_url}")
                text = self.document_processor.download_and_extract_text(document_url)
                if text and len(text.strip()) > 50:
                    return text

            logger.warning("  No valid attachment found")
            return ""

        except Exception as e:
            logger.error(f"  Error getting attachment: {e}")
            return ""

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company
                logger.info("Step 1: Resolving company...")
                company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
                if not company_config:
                    raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
                company_id = company_config['id']
                job_board_url = company_config['jobboard']
                logger.info(f"  Company ID: {company_id}, Board: {job_board_url}")

                city_id = get_city_id(cursor, self.CITY_NAME) if self.CITY_NAME else None

                # Step 2: Load listings page
                logger.info("Step 2: Loading job listings page...")
                page_content = self.selenium_scraper.get_page_content(job_board_url)
                if not page_content:
                    raise Exception("Failed to load job listings page")

                # Step 3: Parse listings
                logger.info("Step 3: Parsing job listings...")
                soup = BeautifulSoup(page_content, 'html.parser')
                job_elements = soup.find_all('ul', class_='postingsList')
                stats['found'] = len(job_elements)
                logger.info(f"  Found {len(job_elements)} job listings")

                if not job_elements:
                    logger.warning("No job listings found")
                    return stats

                # Step 4: Process each job
                logger.info("Step 4: Processing jobs...")
                for i, job_element in enumerate(job_elements):
                    try:
                        logger.info(f"Processing job {i+1}/{len(job_elements)}")

                        job_data = self.extract_job_data_from_listing(job_element, job_board_url)
                        if not job_data.get('posting_url'):
                            logger.warning("  No posting URL, skipping")
                            stats['skipped'] += 1
                            continue

                        if not job_data.get('date_posted'):
                            logger.warning("  No date_posted, skipping")
                            stats['skipped'] += 1
                            continue

                        existing_job_id = check_existing_job_by_url(cursor, job_data['posting_url'])
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        job_description = self.get_job_description_from_attachment(job_element)
                        if not job_description:
                            job_description = (
                                f"Job posting for {job_data.get('job_title', 'position')} "
                                f"— see original posting for details."
                            )

                        store_data = {
                            'job_title': job_data['job_title'],
                            'posting_url': job_data['posting_url'],
                            'posting_id': job_data.get('posting_id'),
                            'job_description': job_description,
                            'date_posted': job_data.get('date_posted'),
                            'scraping_hash': hashlib.md5(
                                f"{job_data['job_title']}{job_data['posting_url']}{job_description}".encode()
                            ).hexdigest(),
                            'function': _map_job_to_function(cursor, job_data.get('position_type', '')),
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, store_data, company_id, self.SOURCE_JOB_BOARD)
                        logger.info(f"  Stored job ID: {job_id}")
                        stats['added'] += 1
                        time.sleep(2.0)

                    except Exception as e:
                        error_msg = f"Error processing job {i+1}: {e}"
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
                _log_scraping_activity(cursor, self.SOURCE_JOB_BOARD, company_id, stats)

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
        scraper = DistrictJobScraper(conn)  # REPLACE class name

        logger.info(f"Starting {DistrictJobScraper.COMPANY_NAME} Applitrack scraping...")
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

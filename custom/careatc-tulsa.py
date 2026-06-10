#!/usr/bin/env python3
"""
careatc-selenium-scrape.py
CareATC Job Board Scraper
Handles CareATC job board with Selenium for full job description extraction
"""

from utils.selenium_config import SeleniumConfig
from utils.posting_operations import store_job_listing, load_active_jobs_cache, check_job_in_cache, update_job_verified_timestamp, mark_stale_jobs_closed
from utils.utility_methods import setup_logging
from utils.company_operations import get_or_create_company, get_company_config_by_name
from utils.db_connection import get_database_connection
from utils.date_utilities import normalize_date_string
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import time
import hashlib
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
from urllib.parse import urljoin

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str = None):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}  # this caches current jobs from db to compare against

    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)
        
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache first, then database"""
        # First check cache
        job_id = check_job_in_cache(job_url, self.active_jobs_cache)
        if job_id:
            return job_id
        # If not in cache, this is a new job
        return None
    
    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing using posting_operations"""
        with self.conn.cursor() as cursor:
            # Add required fields
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'company_id': company_id,
                'city_id': 12  # All CareATC jobs are for Tulsa
            })
        
            return store_job_listing(cursor, enhanced_job_data, company_id, 'CareATC Careers')

    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP 
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")
       
    def log_scraping_activity(self, job_board: str, stats: Dict):
        """Log scraping results"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO ScrapingLog (
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
        
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
        
            # Apply standard timeouts and settings
            SeleniumConfig.setup_driver_timeouts(self.driver)
        
            self.logger.info("Optimized Selenium WebDriver initialized")
        
        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render - optimized for speed"""
        try:
            self.logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            # Shorter, more targeted waits
            wait = WebDriverWait(self.driver, timeout)
            
            # Wait for basic page structure
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                self.logger.warning(f"  Body tag not found within timeout")
                return ""
            
            # Give minimal time for dynamic content
            time.sleep(1.5)
            
            # Get page source
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
            
            # Wait for job listings to load
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '.cc-department-container')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job links within department containers
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, '.cc-department-container a.cc-job-title')
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
            # Job Title and URL
            job_data['job_title'] = job_element.text.strip()
            href = job_element.get_attribute('href')
            self.logger.info(f"  Raw href value: {href}")
            
            # Handle both absolute and relative URLs
            if href.startswith('http'):
                job_data['posting_url'] = href
            else:
                # Handle relative URLs
                base_url = "https://www.careatc.com"
                job_data['posting_url'] = urljoin(base_url, href)
            
            self.logger.info(f"Job {job_number}: {job_data['job_title']}")
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None
    
    def extract_job_description(self, html_content: str) -> str:
        """Extract job description from HTML content"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, navigation
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Get body content
            body = soup.find('body')
            if body:
                # Remove common non-content elements
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
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
    
    def extract_posted_date(self, html_content: str) -> Optional[datetime]:
        """Extract posted date from job page HTML"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Look for divs containing "Posted" text
            posted_divs = soup.find_all('div', string=re.compile(r'Posted', re.IGNORECASE))
            
            for div in posted_divs:
                posted_text = div.get_text(strip=True)
                if 'Posted' in posted_text:
                    self.logger.info(f"  Found posted date text: {posted_text}")
                    return normalize_date_string(posted_text)
            
            # Also check for divs that contain elements with "Posted" text
            posted_divs = soup.find_all('div', class_=re.compile(r'cc-job-description'))
            for div in posted_divs:
                text = div.get_text(strip=True)
                if 'Posted' in text:
                    self.logger.info(f"  Found posted date text in div: {text}")
                    return normalize_date_string(text)
            
            self.logger.warning(f"  No posted date found in job content")
            return None
            
        except Exception as e:
            self.logger.warning(f"Error extracting posted date: {e}")
            return None
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class CareATCSeleniumScraper:
    """CareATC job scraper"""
    COMPANY_NAME = 'CareATC'
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        # Retrieve the website, jobboard, company_id, common_name from company_operations.py
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        # Store company ID for later use
        self.company_id = self.company_config['id']        
        # Configure logging using company name
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
            # Step 1: Company already retrieved during initialization
            self.logger.info("Step 1: Using company from database")
            
            # Step 1.5: Load active jobs cache
            self.logger.info("Step 1.5: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            # Step 2: Get job listings from job board
            self.logger.info("Step 2: Getting job listings from CareATC job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            self.logger.info(f"? Found {len(job_listings)} jobs")
            
            # Step 3: Process each job
            for i, job_metadata in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        # Update timestamp since we found the job on current scrape
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue
                    
                    # Scrape full job description for new jobs
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    
                    # Prepare job data - even if job content fails to load, we still store the job
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url']
                    }
                    
                    if job_html and len(job_html.strip()) >= 100:
                        # Successfully got job content
                        job_description = self.selenium_scraper.extract_job_description(job_html)
                        posted_date = self.selenium_scraper.extract_posted_date(job_html)
                        
                        job_data['job_description'] = job_description
                        job_data['date_posted'] = posted_date if posted_date else datetime.now()
                        
                        self.logger.info(f"  ? Extracted full job content")
                    else:
                        # Job content failed to load, but still store the job
                        job_data['job_description'] = f"Job content could not be loaded. Title: {job_metadata['job_title']}"
                        job_data['date_posted'] = datetime.now()
                        
                        self.logger.warning(f"  Job content failed to load, storing with minimal data")
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ? Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_metadata.get('job_title', 'Unknown')}: {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark stale jobs as closed
            self.logger.info("Step 4: Marking stale jobs as closed...")
            with self.db.conn.cursor() as cursor:
                mark_stale_jobs_closed(cursor, self.company_id, self.logger)
            
            # Step 5: Update company scrape completion
            self.logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)
            
            # Step 6: Log results
            self.logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('CareATC Careers', stats)
            
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
        # Initialize components
        db_manager = DatabaseManager("")
        scraper = CareATCSeleniumScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting CareATC job scraping...")
        results = scraper.scrape_jobs()
        
        # Print summary
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
            print(f"Script failed: {e}")  # Fallback to print if logger not available
        return 1

    finally:
        if scraper:
            scraper.cleanup()
    
    return 0

if __name__ == "__main__":
    exit(main())
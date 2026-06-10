#!/usr/bin/env python3
"""
melton-ultipro-selenium-scrape.py
Melton UltiPro Job Board Scraper
Handles UltiPro job boards with Selenium for full job description extraction
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

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str = None):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}  #this caches current jobs from db to compare against

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
            # Map categorical fields before storing
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'company_id': company_id,
                'job_type_id': self._map_job_type(job_data.get('schedule', '')),
                'function': self._map_job_function(job_data.get('job_category', '')),
                'office_location_id': self._map_office_location(job_data.get('location_type', ''))
            })
        
            return store_job_listing(cursor, enhanced_job_data, company_id, 'Melton UltiPro')

    def _map_job_type(self, schedule: str) -> Optional[int]:
        """Map schedule to job_type_id using LIKE matching"""
        if not schedule:
            return None
            
        schedule_lower = schedule.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{schedule_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{schedule}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            schedule_mappings = {
                'full time': ['full time', 'full-time', 'fulltime'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor'],
            }
            
            for job_type_key, variations in schedule_mappings.items():
                if any(var in schedule_lower for var in variations):
                    cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{schedule}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        self.logger.warning(f"  Could not map '{schedule}' to any job type")
        return None
    
    def _map_job_function(self, job_category: str) -> int:
        """Map job category to function, default to 'Other' (ID 32)"""
        if not job_category:
            return 32
            
        job_category_lower = job_category.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM Functions WHERE LOWER(name) LIKE %s", (f"%{job_category_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{job_category}' to function ID: {result['id']}")
                return result['id']
            
            # Try keyword-based mapping
            function_keywords = {
                'Information Technology': ['technology', 'it', 'software', 'tech', 'data', 'systems'],
                'Engineering': ['engineering', 'engineer'],
                'Finance': ['finance', 'financial', 'accounting'],
                'Human Resources': ['hr', 'human resources', 'people'],
                'Sales': ['sales', 'business development'],
                'Marketing': ['marketing', 'communications'],
                'Operations': ['operations', 'logistics'],
                'Administration': ['admin', 'administrative'],
                'Customer Service': ['customer', 'service', 'support'],
                'Manufacturing': ['manufacturing', 'production'],
                'Quality': ['quality', 'qa', 'qc'],
                'Security': ['security', 'safety'],
                'Legal': ['legal', 'compliance'],
                'Transportation/Logistics': ['driver', 'cdl', 'truck', 'transportation', 'logistics', 'dispatch', 'fleet', 'freight', 'cargo', 'delivery']
            }
            
            for function_name, keywords in function_keywords.items():
                if any(keyword in job_category_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_category}' to function ID: {result['id']} via '{function_name}'")
                        return result['id']
        
        self.logger.info(f"  Mapped '{job_category}' to default function: Other (ID: 32)")
        return 32
    
    def _map_office_location(self, location_type: str) -> int:
        """Map location type to office_location_id, default to 1 (In Office)"""
        if not location_type:
            return 1
            
        location_type_lower = location_type.lower().replace('-', ' ')
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM OfficeLocations WHERE LOWER(REPLACE(name, '-', ' ')) LIKE %s", (f"%{location_type_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{location_type}' to office location ID: {result['id']}")
                return result['id']
            
            # Try common variations
            location_mappings = {
                'remote': ['remote', 'work from home', 'wfh'],
                'hybrid': ['hybrid', 'flexible'],
                'onsite': ['onsite', 'on-site', 'in office', 'office']
            }
            
            for location_key, variations in location_mappings.items():
                if any(var in location_type_lower for var in variations):
                    cursor.execute("SELECT id FROM OfficeLocations WHERE LOWER(name) LIKE %s", (f"%{location_key}%",))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{location_type}' to office location ID: {result['id']} via '{location_key}'")
                        return result['id']
        
        self.logger.info(f"  Mapped '{location_type}' to default office location: In Office (ID: 1)")
        return 1
    
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
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-automation="opportunity"]')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job opportunity divs
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, '[data-automation="opportunity"]')
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
            title_link = job_element.find_element(By.CSS_SELECTOR, '[data-automation="job-title"]')
            job_data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href')
            self.logger.info(f"  Raw href value: {href}")
            # Handle both absolute and relative URLs
            base_url = "https://recruiting.ultipro.com"
            if href.startswith('http'):
                job_data['posting_url'] = href
            else:
                # urljoin handles both '/path' and 'path' correctly
                job_data['posting_url'] = urljoin(base_url + '/', href)

            # Posted Date
            try:
                posted_date_element = job_element.find_element(By.CSS_SELECTOR, '[data-automation="opportunity-posted-date"]')
                job_data['posted_date_raw'] = posted_date_element.text.strip()
                job_data['date_posted'] = normalize_date_string(job_data['posted_date_raw'])
            except NoSuchElementException:
                job_data['posted_date_raw'] = None
                job_data['date_posted'] = None
            
            # Requisition Number (posting_id)
            try:
                req_number_element = job_element.find_element(By.XPATH, './/strong[contains(text(), "Requisition Number")]/following-sibling::span')
                job_data['posting_id'] = req_number_element.text.strip()
            except NoSuchElementException:
                job_data['posting_id'] = None
            
            # Schedule/Hours
            try:
                schedule_element = job_element.find_element(By.CSS_SELECTOR, '[data-automation="job-hours"]')
                job_data['schedule'] = schedule_element.text.strip()
            except NoSuchElementException:
                job_data['schedule'] = None
            
            # Job Category
            try:
                category_element = job_element.find_element(By.CSS_SELECTOR, '[data-automation="job-category"]')
                job_data['job_category'] = category_element.text.strip()
            except NoSuchElementException:
                job_data['job_category'] = None
            
            # Job Location Type (Remote/Hybrid/Onsite)
            try:
                location_type_element = job_element.find_element(By.CSS_SELECTOR, '[data-automation="job-location-type"]')
                job_data['location_type'] = location_type_element.text.strip()
            except NoSuchElementException:
                job_data['location_type'] = None
            
            # Physical Location
            try:
                location_element = job_element.find_element(By.CSS_SELECTOR, '[data-automation="physical-location"]')
                job_data['physical_location'] = location_element.text.strip()
            except NoSuchElementException:
                job_data['physical_location'] = None
            
            self.logger.info(f"Job {job_number}: {job_data['job_title']} - {job_data['posted_date_raw']}")
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
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class MeltonUltiProScraper:
    """Melton UltiPro job scraper"""
    COMPANY_NAME = 'Melton Truck Lines'
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
            self.logger.info("Step 2: Getting job listings from Melton job board...")
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
                        # Only update timestamp if we want to mark this job as still active
                        # For Melton: update timestamp since we found the job on current scrape
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue

                    
                    # Scrape full job description for new jobs only
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        self.logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    job_description = self.selenium_scraper.extract_job_description(job_html)
                    
                    # Prepare complete job data
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': job_metadata['posting_id'],
                        'job_description': job_description,
                        'date_posted': job_metadata['date_posted'],
                        'job_type_id': job_metadata['schedule'],
                        'job_category': job_metadata['job_category'],
                        'location_type': job_metadata['location_type'],
                        'minimum_salary': None,  # UltiPro might not have this
                        'maximum_salary': None,  # UltiPro might not have this
                        'pay_frequency': None,   # UltiPro might not have this
                        'scraping_hash': None
                    }
                    
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
            self.db.log_scraping_activity('Melton UltiPro', stats)
            
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
        scraper = MeltonUltiProScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting Melton UltiPro job scraping...")
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
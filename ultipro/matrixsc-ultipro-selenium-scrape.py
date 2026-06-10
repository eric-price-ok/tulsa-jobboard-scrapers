#!/usr/bin/env python3
"""
matrixsc_ultipro_scraper.py
matrixsc UltiPro Job Board Scraper
Handles UltiPro job boards with Selenium for full job description extraction
"""

from utils.date_utilities import normalize_date_string
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import time
import hashlib
import psycopg
from psycopg.rows import dict_row
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('matrixsc_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn = None
        self.connect()
    
    def connect(self):
        """Establish database connection"""
        try:
            self.conn = psycopg.connect(self.connection_string, row_factory=dict_row)
            self.conn.autocommit = True
            logger.info("Connected to PostgreSQL database")
        except Exception as e:
            logger.error(f"Failed to connect to database: {e}")
            raise
    
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists, update timestamp if found"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s
            """, (job_url,))
            
            existing = cursor.fetchone()
            if existing:
                # Update the updated_at timestamp and skip scraping
                cursor.execute("""
                    UPDATE JobListings 
                    SET updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s
                """, (existing['id'],))
                logger.info(f"  Job already exists (ID: {existing['id']}), updated timestamp")
                return existing['id']
            return None
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Map categorical fields
            job_type_id = self._map_job_type(job_data.get('schedule', ''))
            function = self._map_job_function(job_data.get('job_category', ''))
            office_location_id = self._map_office_location(job_data.get('location_type', ''))
            
            # Insert new job
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, posting_id, company_site_id,
                    source_job_board, date_posted, scraping_hash, 
                    function, job_type_id, office_location_id, minimum_salary, maximum_salary,
                    pay_frequency, approved, city_id, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_id,
                job_data['job_title'],
                job_data['job_description'],
                job_data['posting_url'],
                job_data['posting_id'],
                494,
                'matrixsc UltiPro',
                job_data['date_posted'],
                job_data['scraping_hash'],
                function,
                job_type_id,
                office_location_id,
                job_data.get('minimum_salary'),
                job_data.get('maximum_salary'),
                job_data.get('pay_frequency'),
                False,
                12,
                1
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['job_title']} (ID: {job_id})")
            return job_id
    
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
                logger.info(f"  Mapped '{schedule}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            schedule_mappings = {
                'full time': ['full time', 'full-time', 'fulltime'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor'],
                'temporary': ['temporary', 'temp'],
                'internship': ['intern', 'internship'],
                'seasonal': ['seasonal']
            }
            
            for job_type_key, variations in schedule_mappings.items():
                if any(var in schedule_lower for var in variations):
                    cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped '{schedule}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        logger.warning(f"  Could not map '{schedule}' to any job type")
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
                logger.info(f"  Mapped '{job_category}' to function ID: {result['id']}")
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
                'Legal': ['legal', 'compliance']
            }
            
            for function_name, keywords in function_keywords.items():
                if any(keyword in job_category_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped '{job_category}' to function ID: {result['id']} via '{function_name}'")
                        return result['id']
        
        logger.info(f"  Mapped '{job_category}' to default function: Other (ID: 32)")
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
                logger.info(f"  Mapped '{location_type}' to office location ID: {result['id']}")
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
                        logger.info(f"  Mapped '{location_type}' to office location ID: {result['id']} via '{location_key}'")
                        return result['id']
        
        logger.info(f"  Mapped '{location_type}' to default office location: In Office (ID: 1)")
        return 1
    
    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP 
                WHERE id = %s
            """, (company_id,))
            logger.info(f"Updated last_full_scrape_completed for company {company_id}")
    
    def mark_stale_jobs_closed(self, company_id: int):
        """Mark jobs as closed if not updated during this scrape cycle"""
        with self.conn.cursor() as cursor:
            # Get the last full scrape completion date
            cursor.execute("""
                SELECT last_full_scrape_completed 
                FROM Company 
                WHERE id = %s
            """, (company_id,))
            
            company_data = cursor.fetchone()
            if not company_data or not company_data['last_full_scrape_completed']:
                logger.warning(f"No last_full_scrape_completed date found for company {company_id}")
                return
            
            last_scrape_date = company_data['last_full_scrape_completed']
            
            # Close jobs that weren't updated in this scrape cycle
            cursor.execute("""
                UPDATE JobListings SET 
                    job_status_id = 6,
                    date_closed = CURRENT_DATE
                WHERE company_id = %s 
                AND job_status_id != 6
                AND updated_at < %s
            """, (company_id, last_scrape_date))
            
            closed_count = cursor.rowcount
            if closed_count > 0:
                logger.info(f"Marked {closed_count} stale jobs as closed (status_id = 6)")
    
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
    
    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()
    
    def setup_driver(self):
        """Initialize Chrome WebDriver with optimized options"""
        try:
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument('--headless=new')
            
            # Performance optimizations
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-gpu-sandbox')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--disable-webgl')
            chrome_options.add_argument('--disable-webgl2')
            chrome_options.add_argument('--disable-3d-apis')
            chrome_options.add_argument('--enable-unsafe-swiftshader')
            chrome_options.add_argument('--disable-images')  # Don't load images
            chrome_options.add_argument('--disable-javascript-harmony-shipping')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--disable-plugins-discovery')
            chrome_options.add_argument('--disable-preconnect')
            chrome_options.add_argument('--disable-sync')
            chrome_options.add_argument('--disable-background-timer-throttling')
            chrome_options.add_argument('--disable-renderer-backgrounding')
            chrome_options.add_argument('--disable-backgrounding-occluded-windows')
            chrome_options.add_argument('--disable-client-side-phishing-detection')
            chrome_options.add_argument('--disable-default-apps')
            chrome_options.add_argument('--disable-hang-monitor')
            chrome_options.add_argument('--disable-popup-blocking')
            chrome_options.add_argument('--disable-prompt-on-repost')
            chrome_options.add_argument('--disable-web-security')
            chrome_options.add_argument('--disable-features=TranslateUI,VizDisplayCompositor')
            chrome_options.add_argument('--window-size=1280,720')  # Smaller window
            
            # Disable logging and error messages
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_argument('--disable-gpu-logging')
            chrome_options.add_argument('--disable-extensions-http-throttling')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            # Set page load strategy to eager (don't wait for all resources)
            chrome_options.page_load_strategy = 'eager'
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            # Reduce implicit wait time
            self.driver.implicitly_wait(5)
            
            # Set timeouts
            self.driver.set_page_load_timeout(15)  # Shorter timeout
            self.driver.set_script_timeout(10)
            
            # Execute script to remove automation detection
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            logger.info("Optimized Selenium WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render - optimized for speed"""
        try:
            logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            # Shorter, more targeted waits
            wait = WebDriverWait(self.driver, timeout)
            
            # Wait for basic page structure
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning(f"  Body tag not found within timeout")
                return ""
            
            # Give minimal time for dynamic content
            time.sleep(1.5)
            
            # Get page source
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
                
        except TimeoutException:
            logger.warning(f"  Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""
            
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return ""
    
    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Load job board and extract all job listings"""
        try:
            logger.info(f"Loading job board: {job_board_url}")
            self.driver.get(job_board_url)
            
            # Wait for job listings to load
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '[data-automation="opportunity"]')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job opportunity divs
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, '[data-automation="opportunity"]')
            logger.info(f"Found {len(job_elements)} job opportunities")
            
            jobs = []
            for i, job_element in enumerate(job_elements):
                try:
                    job_data = self.extract_job_metadata(job_element, i + 1)
                    if job_data:
                        jobs.append(job_data)
                except Exception as e:
                    logger.error(f"Error extracting job {i + 1}: {e}")
            
            logger.info(f"Successfully extracted {len(jobs)} job listings")
            return jobs
            
        except TimeoutException:
            logger.error("Timeout waiting for job listings to load")
            return []
        except Exception as e:
            logger.error(f"Error loading job board: {e}")
            return []
    
    def extract_job_metadata(self, job_element, job_number: int) -> Dict:
        """Extract job metadata from a single job element"""
        job_data = {}
        
        try:
            # Job Title and URL
            title_link = job_element.find_element(By.CSS_SELECTOR, '[data-automation="job-title"]')
            job_data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href')
            logger.info(f"  Raw href value: {href}")
            # Handle both absolute and relative URLs
            if href.startswith('http'):
                job_data['posting_url'] = href
            elif href.startswith('/'):
                job_data['posting_url'] = f"https://recruiting2.ultipro.com{href}"
            else:
                job_data['posting_url'] = f"https://recruiting2.ultipro.com/{href}"
                
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
            
            logger.info(f"Job {job_number}: {job_data['job_title']} - {job_data['posted_date_raw']}")
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting metadata for job {job_number}: {e}")
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
                    logger.info(f"  Extracted job description: {len(body_text)} characters")
                    return body_text
            
            logger.warning(f"  No meaningful job description found")
            return html_content
            
        except Exception as e:
            logger.warning(f"Error extracting job description: {e}")
            return html_content
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class matrixscUltiProScraper:
    """matrixsc UltiPro job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        self.company_config = {
            'id': 851,  # matrixsc company ID
            'job_board_url': 'https://recruiting2.ultipro.com/MAT1001MATRX/JobBoard/e2066553-1aa4-4be3-8ce9-1c277556c696/?q=&o=postedDateDesc&w=&wc=&we=&wpst=&f4=rPYg1j3DD0CLccHMjhiamw'
        }
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data['job_title']}{job_data['posting_url']}{job_data.get('job_description', '')}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
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
            company_id = self.company_config['id']
            
            # Step 1: Get job listings from job board
            logger.info("Step 1: Getting job listings from matrixsc job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['job_board_url'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            logger.info(f"? Found {len(job_listings)} jobs")
            
            # Step 2: Process each job
            for i, job_metadata in enumerate(job_listings):
                try:
                    logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        stats['updated'] += 1
                        continue
                    
                    # Scrape full job description for new jobs only
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
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
                        'schedule': job_metadata['schedule'],
                        'job_category': job_metadata['job_category'],
                        'location_type': job_metadata['location_type'],
                        'minimum_salary': None,  # UltiPro might not have this
                        'maximum_salary': None,  # UltiPro might not have this
                        'pay_frequency': None,   # UltiPro might not have this
                        'scraping_hash': self.create_scraping_hash({
                            'job_title': job_metadata['job_title'],
                            'posting_url': job_metadata['posting_url'],
                            'job_description': job_description
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ? Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_metadata.get('job_title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 3: Mark stale jobs as closed
            logger.info("Step 3: Marking stale jobs as closed...")
            self.db.mark_stale_jobs_closed(company_id)
            
            # Step 4: Update company scrape completion
            logger.info("Step 4: Updating company scrape completion...")
            self.db.update_company_scrape_completed(company_id)
            
            # Step 5: Log results
            logger.info("Step 5: Logging results...")
            self.db.log_scraping_activity('matrixsc UltiPro', stats)
            
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
    # Get password from environment variable
    db_password = os.getenv('POSTGRES_PASSWORD')
    if not db_password:
        logger.error("Please set POSTGRES_PASSWORD environment variable")
        logger.error("Example: set POSTGRES_PASSWORD=your_password")
        return 1
    
    db_connection = f"postgresql://postgres:{db_password}@localhost:5432/tulsa_jobs"
    
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = matrixscUltiProScraper(db_manager)
        
        # Run scraping
        logger.info("Starting matrixsc UltiPro job scraping...")
        results = scraper.scrape_jobs()
        
        # Print summary
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
    
    return 0

if __name__ == "__main__":
    exit(main())
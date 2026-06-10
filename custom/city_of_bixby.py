#!/usr/bin/env python3
"""
bixby-jobs-scraper.py
City of Bixby Job Board Scraper
Handles City of Bixby job board with Selenium for full job description extraction
"""

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
        self.active_jobs_cache = {}  # Cache for active jobs by company
    
    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company to reduce database reads"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, posting_url 
                FROM JobListings 
                WHERE company_id = %s AND job_status_id = 1
            """, (company_id,))
            
            results = cursor.fetchall()
            self.active_jobs_cache = {job['posting_url']: job['id'] for job in results}
            self.logger.info(f"Cached {len(self.active_jobs_cache)} active jobs for company {company_id}")
    
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL exists in cache, update timestamp if found"""
        if job_url in self.active_jobs_cache:
            job_id = self.active_jobs_cache[job_url]
            # Update the updated_at timestamp
            with self.conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE JobListings 
                    SET updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s
                """, (job_id,))
            self.logger.info(f"  Job already exists (ID: {job_id}), updated timestamp")
            return job_id
        return None
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Map categorical fields
            job_type_id = self._map_job_type(job_data.get('position_type', ''))
            function = self._map_job_function(job_data.get('job_title', ''))
            office_location_id = 1  # Default to "In Office" for government jobs
            
            # Insert new job
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, posting_id,
                    source_job_board, date_posted, scraping_hash, 
                    function, city_id, job_type_id, office_location_id,
                    approved, job_status_id, 
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         (SELECT id FROM JobStatus WHERE name = 'Active'))
                RETURNING id
            """, (
                company_id,
                job_data['job_title'],
                job_data['job_description'],
                job_data['posting_url'],
                job_data['posting_id'],
                'City of Bixby',
                job_data['date_posted'],
                job_data['scraping_hash'],
                function,
                3,
                job_type_id,
                office_location_id,
                True
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            self.logger.info(f"Created new job: {job_data['job_title']} (ID: {job_id})")
            
            # Add to cache
            self.active_jobs_cache[job_data['posting_url']] = job_id
            
            return job_id
    
    def _map_job_type(self, position_type: str) -> Optional[int]:
        """Map position type to job_type_id using LIKE matching"""
        if not position_type:
            return None
            
        position_type_lower = position_type.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{position_type_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{position_type}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            position_mappings = {
                'full time': ['full time', 'full-time', 'fulltime', 'permanent', 'regular'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor', 'contractual']
            }
            
            for job_type_key, variations in position_mappings.items():
                if any(var in position_type_lower for var in variations):
                    cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{position_type}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        self.logger.warning(f"  Could not map '{position_type}' to any job type")
        return None
    
    def _map_job_function(self, job_title: str) -> int:
        """Map job title to function, default to 'Other' (ID 32)"""
        if not job_title:
            return 32
            
        job_title_lower = job_title.lower()
        
        with self.conn.cursor() as cursor:
            # Try keyword-based mapping for government positions
            function_keywords = {
                'Information Technology': ['technology', 'it', 'software', 'tech', 'data', 'systems', 'computer', 'network'],
                'Engineering': ['engineering', 'engineer', 'civil', 'mechanical', 'electrical'],
                'Finance': ['finance', 'financial', 'accounting', 'accountant', 'budget', 'treasurer'],
                'Human Resources': ['hr', 'human resources', 'people', 'personnel'],
                'Administration': ['admin', 'administrative', 'clerk', 'coordinator', 'assistant', 'secretary'],
                'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'paralegal'],
                'Public Safety': ['police', 'fire', 'safety', 'security', 'emergency', 'dispatcher'],
                'Operations': ['operations', 'maintenance', 'facility', 'utilities', 'public works'],
                'Planning': ['planning', 'planner', 'zoning', 'development'],
                'Parks and Recreation': ['parks', 'recreation', 'athletic', 'grounds', 'maintenance'],
                'Customer Service': ['customer', 'service', 'support', 'reception', 'front desk'],
                'Transportation/Logistics': ['transportation', 'logistics', 'fleet', 'driver'],
                'Healthcare Provider': ['nurse', 'medical', 'healthcare', 'clinical'],
                'Skilled Labor': ['operator', 'technician', 'maintenance', 'mechanic', 'welder', 'electrician']
            }
            
            for function_name, keywords in function_keywords.items():
                if any(keyword in job_title_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_title}' to function ID: {result['id']} via '{function_name}'")
                        return result['id']
        
        self.logger.info(f"  Mapped '{job_title}' to default function: Other (ID: 32)")
        return 32
    
    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP 
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")
    
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
                self.logger.warning(f"No last_full_scrape_completed date found for company {company_id}")
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
                self.logger.info(f"Marked {closed_count} stale jobs as closed (status_id = 6)")
    
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
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument('--headless=new')
            
            # Performance optimizations
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-images')  # Don't load images
            chrome_options.add_argument('--disable-javascript-harmony-shipping')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-plugins')
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
            chrome_options.add_argument('--window-size=1280,720')
            
            # Disable logging and error messages
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            # Set page load strategy to eager
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
            self.driver.set_page_load_timeout(15)
            self.driver.set_script_timeout(10)
            
            # Execute script to remove automation detection
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            self.logger.info("Optimized Selenium WebDriver initialized")
            
        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            self.logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            # Wait for basic page structure
            wait = WebDriverWait(self.driver, timeout)
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
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.job')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job divs
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, 'div.job')
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
            title_link = job_element.find_element(By.CSS_SELECTOR, 'a[id^="jobTitle_"]')
            job_data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href')
            
            # Build full URL
            if href.startswith('http'):
                job_data['posting_url'] = href
            elif href.startswith('/'):
                job_data['posting_url'] = f"https://www.bixbyok.gov{href}"
            else:
                job_data['posting_url'] = f"https://www.bixbyok.gov/{href}"
            
            # Extract JobID from href for posting_id
            job_id_match = re.search(r'JobID=([^&]+)', href)
            job_data['posting_id'] = job_id_match.group(1) if job_id_match else None
            
            # Posted Date (from hidden span)
            try:
                posted_date_element = job_element.find_element(By.CSS_SELECTOR, 'span[id^="jobStartDate_"]')
                job_data['posted_date_raw'] = posted_date_element.text.strip()
                job_data['date_posted'] = normalize_date_string(job_data['posted_date_raw'])
            except NoSuchElementException:
                job_data['posted_date_raw'] = None
                job_data['date_posted'] = None
            
            self.logger.info(f"Job {job_number}: {job_data['job_title']} - {job_data['posted_date_raw']}")
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None
    
    def extract_job_description_and_details(self, html_content: str) -> Dict:
        """Extract job description and position type from HTML content"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, navigation
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Get body content for job description
            body = soup.find('body')
            job_description = ""
            position_type = None
            
            if body:
                # Remove common non-content elements
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()
                
                body_text = body.get_text(separator=' ', strip=True)
                if len(body_text) > 100:
                    job_description = body_text
                    self.logger.info(f"  Extracted job description: {len(job_description)} characters")
                
                # Extract Position Type
                position_type_match = re.search(r'Position Type[:\s]+([^\n\r]+)', body_text, re.IGNORECASE)
                if position_type_match:
                    position_type = position_type_match.group(1).strip()
                    # Clean up common suffixes
                    position_type = re.sub(r'\s*(Department|Location|Salary).*$', '', position_type, flags=re.IGNORECASE)
                    self.logger.info(f"  Extracted Position Type: {position_type}")
            
            return {
                'job_description': job_description or html_content,
                'position_type': position_type
            }
            
        except Exception as e:
            self.logger.warning(f"Error extracting job details: {e}")
            return {
                'job_description': html_content,
                'position_type': None
            }
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class BixbyJobScraper:
    """City of Bixby job scraper"""
    COMPANY_NAME = 'City of Bixby'
    
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
        
        # Load active jobs cache
        self.db.load_active_jobs_cache(self.company_id)

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
            # Step 1: Company already retrieved during initialization
            self.logger.info("Step 1: Using company from database")
            
            # Step 2: Get job listings from job board
            self.logger.info("Step 2: Getting job listings from Bixby job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            self.logger.info(f"? Found {len(job_listings)} jobs")
            
            # Step 3: Process each job
            for i, job_metadata in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")
                    
                    # Check if job already exists using cache
                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        stats['updated'] += 1
                        continue
                    
                    # Scrape full job description for new jobs only
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        self.logger.warning(f"  Failed to get meaningful job content, storing with limited info")
                        # Store with limited information
                        job_details = {
                            'job_description': f"Job Title: {job_metadata['job_title']}\nPosted: {job_metadata.get('posted_date_raw', 'Unknown')}",
                            'position_type': None
                        }
                    else:
                        job_details = self.selenium_scraper.extract_job_description_and_details(job_html)
                    
                    # Prepare complete job data
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': job_metadata['posting_id'],
                        'job_description': job_details['job_description'],
                        'date_posted': job_metadata['date_posted'],
                        'position_type': job_details['position_type'],
                        'scraping_hash': self.create_scraping_hash({
                            'job_title': job_metadata['job_title'],
                            'posting_url': job_metadata['posting_url'],
                            'job_description': job_details['job_description']
                        })
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
            self.db.mark_stale_jobs_closed(self.company_id)
            
            # Step 5: Update company scrape completion
            self.logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)
            
            # Step 6: Log results
            self.logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('City of Bixby', stats)
            
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
        scraper = BixbyJobScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting City of Bixby job scraping...")
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
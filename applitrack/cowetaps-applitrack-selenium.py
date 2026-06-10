#!/usr/bin/env python3
"""
cowetaps-applitrack-selenium-scrape.py
Coweta Public Schools Applitrack Job Board Scraper
Scrapes job listings and downloads attached job descriptions
"""

from utils.date_utilities import normalize_date_string
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import time
import hashlib
import psycopg
from psycopg.rows import dict_row
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import requests
import os


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('coweta_scraper.log', encoding='utf-8'),
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
    
    def check_existing_job(self, posting_url: str) -> Optional[int]:
        """Check if job URL already exists, update timestamp if found"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s
            """, (posting_url,))
            
            existing = cursor.fetchone()
            if existing:
                # Update the updated_at timestamp
                cursor.execute("""
                    UPDATE JobListings 
                    SET updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s
                """, (existing['id'],))
                logger.info(f"  Job already exists (ID: {existing['id']}), updated timestamp")
                return existing['id']
            return None
    
    def store_job_listing(self, job_data: Dict) -> int:
        """Store new job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Map job title to function
            function = self._map_job_to_function(job_data.get('position_type', ''))
            
            # Insert new job
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, 
                    source_job_board, date_posted, scraping_hash, 
                    function, posting_id, city_id, approved, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         (SELECT id FROM JobStatus WHERE name = 'Active'))
                RETURNING id
            """, (
                659,  # Hardcoded company_id for Coweta Public Schools
                job_data['job_title'],
                job_data['job_description'],
                job_data['posting_url'],
                'Coweta Applitrack',
                job_data['date_posted'],
                job_data['scraping_hash'],
                function,
                job_data['posting_id'],
                6,  # city_id for Coweta
                True
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['job_title']} (ID: {job_id})")
            return job_id
    
    def _map_job_to_function(self, position_type: str) -> Optional[int]:
        """Map position type to function ID using keywords"""
        if not position_type:
            return self._get_other_function()
        
        position_lower = position_type.lower()
        
        # Define function mapping keywords for school positions
        function_keywords = {
            'Administration': [
                'principal', 'assistant principal', 'superintendent', 'director', 
                'coordinator', 'supervisor', 'admin', 'leadership', 'administration'
            ],
            'Education/Training': [
                'teacher', 'instructor', 'educator', 'faculty', 'classroom',
                'math teacher', 'science teacher', 'english teacher', 'social studies',
                'special education', 'art teacher', 'music teacher', 'pe teacher',
                'librarian', 'counselor', 'speech', 'occupational therapist'
            ],
            'Other': [
                'aide', 'assistant', 'paraprofessional', 'secretary', 'clerk',
                'custodian', 'maintenance', 'bus driver', 'cafeteria', 'food service',
                'nurse', 'health', 'security', 'monitor'
            ],
            'Information Technology': [
                'technology', 'it', 'computer', 'network', 'systems', 'tech support'
            ],
            'Transportation/Logistics': [
                'bus', 'driver', 'transportation', 'mechanic', 'fleet'
            ],
            'Accounting': [
                'business manager', 'finance clerk', 'accounting', 'bookkeeper', 'payroll'
            ],
            'Security': [
                'security', 'safety', 'sro', 'resource officer', 'guard'
            ],
            'Skilled Labor': [
                'maintenance', 'custodial', 'janitor', 'groundskeeper', 'facilities'
            ]
        }
        
        # Try to match keywords
        for function_name, keywords in function_keywords.items():
            for keyword in keywords:
                if keyword in position_lower:
                    # Get function ID from database
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            logger.info(f"  Mapped '{position_type}' to function: {function_name}")
                            return result['id']
        
        # Default to 'Other' if no match found
        return self._get_other_function()
    
    def _get_other_function(self) -> Optional[int]:
        """Get the 'Other' function ID"""
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM Functions WHERE name = %s", ('Other',))
            result = cursor.fetchone()
            if result:
                return result['id']
        return None
    
    def update_company_scrape_completed(self):
        """Update last_full_scrape_completed timestamp for Coweta Public Schools"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP 
                WHERE id = 659
            """)
            logger.info("Updated last_full_scrape_completed for Coweta Public Schools")
    
    def mark_stale_jobs_closed(self):
        """Mark jobs as closed if not updated during this scrape cycle"""
        with self.conn.cursor() as cursor:
            # Get the last full scrape completion date
            cursor.execute("""
                SELECT last_full_scrape_completed 
                FROM Company 
                WHERE id = 659
            """)
            
            company_data = cursor.fetchone()
            if not company_data or not company_data['last_full_scrape_completed']:
                logger.warning("No last_full_scrape_completed date found for Coweta Public Schools")
                return
            
            last_scrape_date = company_data['last_full_scrape_completed']
            
            # Close jobs that weren't updated in this scrape cycle
            cursor.execute("""
                UPDATE JobListings SET 
                    job_status_id = 6,
                    date_closed = CURRENT_DATE
                WHERE company_id = 659
                AND job_status_id != 6
                AND updated_at < %s
            """, (last_scrape_date,))
            
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
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--disable-javascript-harmony-shipping')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--window-size=1280,720')
            
            # Disable logging
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(15)
            
            logger.info("Selenium WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_page_content(self, url: str, timeout=12) -> str:
        """Load page and return content"""
        try:
            logger.info(f"Loading page: {url}")
            self.driver.get(url)
            
            wait = WebDriverWait(self.driver, timeout)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # Give time for dynamic content
            #time.sleep(2) - not needed with inline job descriptions - all content on main page
            
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
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class CowetaJobScraper:
    """Coweta Public Schools Applitrack job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        self.job_board_url = "https://www.applitrack.com/cowetaps/onlineapp/default.aspx?all=1"
    
    def extract_job_id_and_url(self, job_element) -> tuple:
        """Extract JobID from title2 span"""
        job_id = None
        
        # Extract from title2 span
        title2_span = job_element.find('span', class_='title2')
        if title2_span:
            text = title2_span.get_text()
            match = re.search(r'JobID\s*:?\s*(\d+)', text, re.IGNORECASE)
            if match:
                job_id = match.group(1)
                logger.info(f"  Found JobID from title2 span: {job_id}")
        
        if job_id:
            job_url = f"https://www.applitrack.com/cowetaps/onlineapp/default.aspx?all=1&AppliTrackJobId={job_id}&AppliTrackLayoutMode=detail&AppliTrackViewPosting=1"
            return job_id, job_url
        
        logger.warning("  Could not extract JobID from job element")
        return None, None
    
    def extract_job_data_from_listing(self, job_element) -> Dict:
        """Extract job data from a single job listing element"""
        job_data = {}
        
        try:
            logger.info("  Starting job data extraction...")
            
            # Extract job title from td id="wrapword"
            title_td = job_element.find('td', id='wrapword')
            if title_td:
                job_data['job_title'] = title_td.get_text(strip=True)
                logger.info(f"  Extracted job title: '{job_data['job_title']}'")
            else:
                logger.warning("  No td with id='wrapword' found")
            
            # Extract JobID and build URL
            job_id, job_url = self.extract_job_id_and_url(job_element)
            if job_id and job_url:
                job_data['posting_id'] = job_id
                job_data['posting_url'] = job_url
                logger.info(f"  Extracted posting_id: {job_id}")
                logger.info(f"  Built posting_url: {job_url}")
            else:
                logger.warning("  Failed to extract JobID and URL")
            
            # Get all li elements for date and position extraction
            list_items = job_element.find_all('li')
            logger.info(f"  Found {len(list_items)} li elements total")
            
            # Extract date posted
            logger.info("  Starting date extraction...")
            date_found = False
            for i, li in enumerate(list_items):
                # Look for Date Posted in label span within this li
                label_span = li.find('span', class_='label')
                if label_span and 'Date Posted' in label_span.get_text():
                    logger.info(f"  *** Found Date Posted label in li {i+1} ***")
                    normal_span = li.find('span', class_='normal')
                    if normal_span:
                        date_text = normal_span.get_text(strip=True)
                        logger.info(f"  *** Found date in normal span: '{date_text}' ***")
                        job_data['date_posted'] = normalize_date_string(date_text)
                        date_found = True
                        break
                    else:
                        logger.warning("  Found Date Posted label but no normal span")
            
            if not date_found:
                logger.error("  *** CRITICAL: No 'Date Posted' found in job element ***")
            
            # Extract position type
            logger.info("  Starting position type extraction...")
            position_found = False
            for i, li in enumerate(list_items):
                # Look for Position Type in label span within this li
                label_span = li.find('span', class_='label')
                if label_span and 'Position Type' in label_span.get_text():
                    logger.info(f"  *** Found Position Type label in li {i+1}: '{label_span.get_text()}' ***")
                    normal_span = li.find('span', class_='normal')
                    if normal_span:
                        position_text = normal_span.get_text(strip=True)
                        logger.info(f"  *** Found position type in normal span: '{position_text}' ***")
                        job_data['position_type'] = position_text
                        position_found = True
                        break
                    else:
                        logger.warning("  Found Position Type label but no normal span")
            
            if not position_found:
                logger.warning("  *** No 'Position Type' found in job element ***")
            
            logger.info(f"  Final job_data keys: {list(job_data.keys())}")
            for key, value in job_data.items():
                logger.info(f"    {key}: {value}")
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting job data: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return {}
    
    def get_job_description_from_wordsection(self, job_element) -> str:
        """Get job description from WordSection1 class within the job element"""
        try:
            wordsection_div = job_element.find('div', class_='WordSection1')
            if wordsection_div:
                job_description = wordsection_div.get_text(strip=True)
                if job_description and len(job_description.strip()) > 50:
                    logger.info(f"  Extracted job description from WordSection1: {len(job_description)} characters")
                    return job_description
            
            logger.warning("  No WordSection1 found or content too short")
            return "Job description not available. Please see original posting for details."
            
        except Exception as e:
            logger.error(f"  Error extracting job description from WordSection1: {e}")
            return "Job description not available. Please see original posting for details."
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data.get('job_title', '')}{job_data.get('posting_url', '')}{job_data.get('job_description', '')}"
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
            # Step 1: Load the main job listings page
            logger.info("Step 1: Loading job listings page...")
            page_content = self.selenium_scraper.get_page_content(self.job_board_url)
            if not page_content:
                raise Exception("Failed to load job listings page")
            
            # Step 2: Parse job listings
            logger.info("Step 2: Parsing job listings...")
            soup = BeautifulSoup(page_content, 'html.parser')
            
            # Find job listings - look for ul with class "postingsList"
            job_elements = soup.find_all('ul', class_='postingsList')
            logger.info(f"Found {len(job_elements)} job listings")
            stats['found'] = len(job_elements)
            
            if len(job_elements) == 0:
                logger.warning("No job listings found")
                return stats
            
            # Step 3: Process each job
            logger.info(f"Processing all {len(job_elements)} jobs")

            for i, job_element in enumerate(job_elements):
                try:
                    logger.info(f"Processing job {i+1}/{len(job_elements)}")
                    
                    # Extract basic job data from listing
                    job_data = self.extract_job_data_from_listing(job_element)
                    if not job_data.get('posting_url'):
                        logger.warning("  No posting URL found, skipping")
                        stats['skipped'] += 1
                        continue
                    
                    logger.info(f"  Job: {job_data.get('job_title', 'Unknown')}")
                    logger.info(f"  URL: {job_data.get('posting_url')}")
                    logger.info(f"  Date Posted: {job_data.get('date_posted')}")
                    logger.info(f"  Position Type: {job_data.get('position_type')}")
                    
                    # Check for required fields
                    if not job_data.get('date_posted'):
                        logger.error("  ? SKIPPING: No date_posted found")
                        stats['skipped'] += 1
                        continue
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_data['posting_url'])
                    if existing_job_id:
                        logger.info(f"  Job already exists (ID: {existing_job_id}), skipping detail scraping")
                        stats['updated'] += 1
                        continue
                    
                    # Get job description from WordSection1 (no need to visit detail page)
                    logger.info("  This is a new job, getting job description from WordSection1...")
                    job_description = self.get_job_description_from_wordsection(job_element)
                    if not job_description or len(job_description.strip()) < 50:
                        logger.warning("  Failed to get meaningful job description")
                        # Still store the job but with limited description
                        job_description = "Job description not available. Please see original posting for details."
                    
                    # Complete job data
                    job_data['job_description'] = job_description
                    job_data['scraping_hash'] = self.create_scraping_hash(job_data)
                    
                    # Log all job data before storing
                    logger.info("  Final job data before database storage:")
                    for key, value in job_data.items():
                        if key == 'job_description':
                            logger.info(f"    {key}: {len(str(value))} characters")
                        else:
                            logger.info(f"    {key}: {value}")
                    
                    # Store job in database
                    logger.info("  Attempting to store job in database...")
                    job_id = self.db.store_job_listing(job_data)
                    logger.info(f"  ? Successfully stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(2.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {i+1}: {e}"
                    logger.error(error_msg)
                    import traceback
                    logger.error(f"Full traceback: {traceback.format_exc()}")
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark stale jobs as closed
            logger.info("Step 4: Marking stale jobs as closed...")
            self.db.mark_stale_jobs_closed()
            
            # Step 5: Update company scrape completion
            logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed()
            
            # Step 6: Log results
            logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('Coweta Applitrack', stats)
            
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
    
    # Database configuration - works for both dev and production
    db_host = os.getenv('POSTGRES_HOST', 'localhost')
    db_port = os.getenv('POSTGRES_PORT', '5432')
    db_name = os.getenv('POSTGRES_DB', 'tulsa_jobs')
    db_user = os.getenv('POSTGRES_USER', 'postgres')
    db_password = os.getenv('POSTGRES_PASSWORD')

    if not db_password:
        logger.error("Please set POSTGRES_PASSWORD environment variable")
        logger.error("Example: set POSTGRES_PASSWORD=your_password")
        return 1

    db_connection = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = CowetaJobScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Coweta Public Schools job scraping...")
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
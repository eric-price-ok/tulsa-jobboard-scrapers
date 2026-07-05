#!/usr/bin/env python3
"""
alert360_paycom_scraper.py
Alert 360 Paycom Job Board Scraper
Handles Paycom API with Selenium for salary extraction
"""

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
import requests
import json
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('alert360_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn = None
        self.company_site_cache = {}  # Cache for company sites by company_id
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
    
    def load_company_sites_cache(self, company_id: int):
        """Load all company sites for a given company into cache"""
        if company_id in self.company_site_cache:
            return  # Already cached
        
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, shortname FROM CompanySite 
                WHERE company_id = %s
            """, (company_id,))
            
            sites = cursor.fetchall()
            # Create a dictionary mapping shortname to id
            self.company_site_cache[company_id] = {site['shortname']: site['id'] for site in sites}
            logger.info(f"Loaded {len(sites)} company sites into cache for company {company_id}")
    
    def get_or_create_company_site(self, company_id: int, location_description: str) -> int:
        """Get existing company site ID or create new one based on location description"""
        # Ensure cache is loaded for this company
        self.load_company_sites_cache(company_id)
        
        # Check cache first
        if location_description in self.company_site_cache[company_id]:
            site_id = self.company_site_cache[company_id][location_description]
            logger.info(f"  Found cached company site ID: {site_id} for '{location_description}'")
            return site_id
        
        # Not in cache, create new company site
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO CompanySite (company_id, shortname, site_type, city, is_headquarters, is_active)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (company_id, location_description, 1, "Tulsa", True, True))
            
            result = cursor.fetchone()
            new_site_id = result['id']
            
            # Add to cache
            self.company_site_cache[company_id][location_description] = new_site_id
            
            logger.info(f"  Created new company site ID: {new_site_id} for '{location_description}'")
            return new_site_id
    
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
            job_type_id = self._map_job_type(job_data.get('position_description', ''))
            function = self._map_job_function(job_data.get('department', ''))
            office_location_id = 1  # Default to In Office
            
            # Insert new job
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, posting_id, company_site_id,
                    source_job_board, scraping_hash, 
                    function, job_type_id, office_location_id, minimum_salary, maximum_salary,
                    pay_frequency, approved, city_id, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_id,
                job_data['job_title'],
                job_data['job_description'],
                job_data['posting_url'],
                job_data['posting_id'],
                job_data['company_site_id'],
                'Alert 360 Paycom',
                job_data['scraping_hash'],
                function,
                job_type_id,
                office_location_id,
                job_data.get('minimum_salary'),
                job_data.get('maximum_salary'),
                job_data.get('pay_frequency'),
                False,
                12,  # Tulsa city_id
                1    # Active status
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['job_title']} (ID: {job_id})")
            return job_id
    
    def _map_job_type(self, position_description: str) -> int:
        """Map position description to job_type_id, default to Full Time (3)"""
        if not position_description:
            return 3  # Default to Full Time
            
        position_lower = position_description.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{position_lower}%",))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped '{position_description}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            position_mappings = {
                'full time': ['full time', 'full-time', 'fulltime'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor'],
            }
            
            for job_type_key, variations in position_mappings.items():
                if any(var in position_lower for var in variations):
                    cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped '{position_description}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        logger.info(f"  Mapped '{position_description}' to default job type: Full Time (ID: 3)")
        return 3  # Default to Full Time
    
    def _map_job_function(self, department: str) -> int:
        """Map department to function, default to 'Other' (ID 32)"""
        if not department:
            return 32
            
        department_lower = department.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM Functions WHERE LOWER(name) LIKE %s", (f"%{department_lower}%",))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped '{department}' to function ID: {result['id']}")
                return result['id']
            
            # Try keyword-based mapping
            function_keywords = {
                'Information Technology': ['technology', 'software', 'data', 'systems', 'network'],
                'Engineering': ['engineering', 'engineer'],
                'Accounting': ['finance', 'financial', 'accounting'],
                'Human Resources': ['hr', 'human resources', 'people'],
                'Sales': ['sales', 'business development'],
                'Marketing': ['marketing', 'communications'],
                'Operations': ['operations', 'logistics'],
                'Administrative': ['admin', 'administrative'],
                'Customer Support': ['customer', 'service', 'support'],
                'Manufacturing': ['manufacturing', 'production'],
                'Quality': ['quality', 'qa', 'qc'],
                'Security': ['security', 'safety'],
                'Legal': ['legal', 'compliance']
            }
            
            for function_name, keywords in function_keywords.items():
                if any(keyword in department_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped '{department}' to function ID: {result['id']} via '{function_name}'")
                        return result['id']
        
        logger.info(f"  Mapped '{department}' to default function: Other (ID: 32)")
        return 32
    
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
    """Handles JavaScript-heavy job pages using Selenium for salary extraction"""
    
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
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--window-size=1280,720')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            # Set timeouts
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(15)
            self.driver.set_script_timeout(10)
            
            logger.info("Selenium WebDriver initialized for salary extraction")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def extract_salary_from_job_page(self, job_url: str) -> Dict:
        """Extract salary information from job detail page"""
        salary_info = {
            'minimum_salary': None,
            'maximum_salary': None,
            'pay_frequency': None
        }
        
        try:
            logger.info(f"  Loading job page for salary extraction...")
            self.driver.get(job_url)
            
            # Wait for page to load
            wait = WebDriverWait(self.driver, 10)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # Give time for dynamic content
            time.sleep(2)
            
            # Look for salary span
            try:
                salary_span = self.driver.find_element(By.CSS_SELECTOR, 'span[name="level"][aria-label="Salary Range"]')
                salary_text = salary_span.text.strip()
                logger.info(f"  Found salary text: '{salary_text}'")
                
                # Parse salary text (e.g., "$17.00 - $18.00 Hourly")
                salary_info = self._parse_salary_text(salary_text)
                
            except NoSuchElementException:
                logger.info(f"  No salary span found on job page")
                
        except Exception as e:
            logger.warning(f"  Error extracting salary from job page: {e}")
        
        return salary_info
    
    def _parse_salary_text(self, salary_text: str) -> Dict:
        """Parse salary text and extract min, max, and frequency"""
        salary_info = {
            'minimum_salary': None,
            'maximum_salary': None,
            'pay_frequency': None
        }
        
        try:
            # Remove dollar signs and commas for processing
            clean_text = salary_text.replace('$', '').replace(',', '')
            
            # Look for salary range pattern (e.g., "17.00 - 18.00 Hourly")
            range_pattern = r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)\s+(Hourly|Annual|Monthly|Weekly)'
            match = re.search(range_pattern, clean_text, re.IGNORECASE)
            
            if match:
                min_salary = float(match.group(1))
                max_salary = float(match.group(2))
                frequency = match.group(3).title()
                
                # Convert to integers (assuming we store as cents or whole dollars)
                salary_info['minimum_salary'] = int(min_salary * 100) if frequency.lower() == 'hourly' else int(min_salary)
                salary_info['maximum_salary'] = int(max_salary * 100) if frequency.lower() == 'hourly' else int(max_salary)
                salary_info['pay_frequency'] = frequency
                
                logger.info(f"  Parsed salary: ${min_salary} - ${max_salary} {frequency}")
            else:
                # Try single salary pattern
                single_pattern = r'(\d+\.?\d*)\s+(Hourly|Annual|Monthly|Weekly)'
                single_match = re.search(single_pattern, clean_text, re.IGNORECASE)
                
                if single_match:
                    salary = float(single_match.group(1))
                    frequency = single_match.group(2).title()
                    
                    salary_info['minimum_salary'] = int(salary * 100) if frequency.lower() == 'hourly' else int(salary)
                    salary_info['maximum_salary'] = salary_info['minimum_salary']
                    salary_info['pay_frequency'] = frequency
                    
                    logger.info(f"  Parsed single salary: ${salary} {frequency}")
                else:
                    logger.warning(f"  Could not parse salary format: '{salary_text}'")
                    
        except Exception as e:
            logger.warning(f"  Error parsing salary text '{salary_text}': {e}")
        
        return salary_info
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class Alert360PaycomScraper:
    """Alert 360 Paycom job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        self.company_config = {
            'id': 530,  # Alert 360 company ID
            'api_url': 'https://www.paycomonline.net/v4/ats/web.php/jobs?partial=1&clientkey=0A65361DBC63DE205ED0334BD3D0E4D5',
            'job_board_url': 'https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=0A65361DBC63DE205ED0334BD3D0E4D5'
        }
        
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=0A65361DBC63DE205ED0334BD3D0E4D5'
        })
    
    def get_api_jobs(self) -> List[Dict]:
        """Fetch all jobs from Paycom API"""
        try:
            logger.info("Fetching jobs from Paycom API...")
            response = self.session.get(self.company_config['api_url'], timeout=30)
            response.raise_for_status()
            
            jobs_data = response.json()
            logger.info(f"✓ Retrieved {len(jobs_data)} jobs from API")
            return jobs_data
            
        except Exception as e:
            logger.error(f"Error fetching jobs from API: {e}")
            return []
    
    def filter_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter jobs for Tulsa locations"""
        tulsa_jobs = []
        
        logger.info(f"Filtering {len(jobs)} jobs for Tulsa locations...")
        
        for job in jobs:
            location = job.get('location', {})
            location_desc = location.get('description', '')
            
            if 'Tulsa' in location_desc:
                tulsa_jobs.append(job)
                logger.info(f"  ✓ Tulsa job: {job.get('title', 'Unknown')} - {location_desc}")
            else:
                logger.debug(f"  ✗ Non-Tulsa job: {job.get('title', 'Unknown')} - {location_desc}")
        
        logger.info(f"✓ Found {len(tulsa_jobs)} Tulsa jobs")
        return tulsa_jobs
    
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
            
            # Step 1: Get jobs from API
            logger.info("Step 1: Getting jobs from Paycom API...")
            all_jobs = self.get_api_jobs()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            
            # Step 2: Filter for Tulsa jobs
            logger.info("Step 2: Filtering for Tulsa jobs...")
            tulsa_jobs = self.filter_tulsa_jobs(all_jobs)
            if not tulsa_jobs:
                logger.warning("No Tulsa jobs found")
                return stats
            
            stats['found'] = len(tulsa_jobs)
            
            # Step 3: Process each Tulsa job
            for i, job in enumerate(tulsa_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(tulsa_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Build job URL
                    job_url_path = job.get('url', '')
                    if not job_url_path:
                        logger.warning(f"  No URL path found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://www.paycomonline.net{job_url_path}"
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_url)
                    if existing_job_id:
                        stats['updated'] += 1
                        continue
                    
                    # Extract salary information
                    salary_info = self.selenium_scraper.extract_salary_from_job_page(job_url)
                    
                    # Get location description and create company site
                    location = job.get('location', {})
                    location_description = location.get('description', 'Alert 360 - Tulsa, OK')
                    company_site_id = self.db.get_or_create_company_site(company_id, location_description)
                    
                    # Prepare job data
                    description = job.get('description', '')
                    qualifications = job.get('qualifications', '')
                    full_description = f"{description}\n\nQualifications:\n{qualifications}".strip()
                    
                    job_data = {
                        'job_title': job.get('title', ''),
                        'posting_url': job_url,
                        'posting_id': str(job.get('jobcode', '')),
                        'job_description': full_description,
                        'position_description': job.get('position_description', ''),
                        'department': job.get('deptcode', ''),
                        'company_site_id': company_site_id,
                        'minimum_salary': salary_info.get('minimum_salary'),
                        'maximum_salary': salary_info.get('maximum_salary'),
                        'pay_frequency': salary_info.get('pay_frequency'),
                        'scraping_hash': ''  # Will be set below
                    }
                    
                    # Create scraping hash
                    job_data['scraping_hash'] = self.create_scraping_hash(job_data)
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark stale jobs as closed
            logger.info("Step 4: Marking stale jobs as closed...")
            self.db.mark_stale_jobs_closed(company_id)
            
            # Step 5: Update company scrape completion
            logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(company_id)
            
            # Step 6: Log results
            logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('Alert 360 Paycom', stats)
            
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
        scraper = Alert360PaycomScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Alert 360 Paycom job scraping...")
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
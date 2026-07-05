#!/usr/bin/env python3
"""
ameristar_assaabloy_scraper.py
Ameristar/Assa Abloy Job Board Scraper
Scrapes jobs from Assa Abloy REST API and filters for Tulsa positions
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
        logging.FileHandler('ameristar_scraper.log', encoding='utf-8'),
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
            function = self._map_job_function(job_data.get('job_title', ''))
            experience_id = self._map_experience_level(job_data.get('experience_level_name', ''))
            
            # Insert new job
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, posting_id,
                    source_job_board, date_posted, date_closed, scraping_hash, 
                    function, experience_id, first_shift, second_shift, third_shift,
                    company_site_id, approved, city_id, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_id,
                job_data['job_title'],
                job_data['job_description'],
                job_data['posting_url'],
                job_data['posting_id'],
                'Assa Abloy API',
                job_data['date_posted'],
                job_data.get('date_closed'),
                job_data['scraping_hash'],
                function,
                experience_id,
                job_data.get('first_shift', True),
                job_data.get('second_shift', False),
                job_data.get('third_shift', False),
                489,
                False,
                12,  # Tulsa city_id
                1    # Active status
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['job_title']} (ID: {job_id})")
            return job_id
    
    def _map_experience_level(self, experience_name: str) -> int:
        """Map experience level name to experience_id"""
        experience_mapping = {
            "": 7,
            "Student": 1,
            "Entry Level": 2,
            "Associate": 3,
            "Mid Senior": 4,
            "Senior": 5,
            "Director": 6
        }
        
        experience_id = experience_mapping.get(experience_name, 7)  # Default to 7 if not found
        logger.info(f"  Mapped experience '{experience_name}' to ID: {experience_id}")
        return experience_id
    
    def _map_job_function(self, job_title: str) -> int:
        """Map job title to function ID using manufacturing-focused keywords"""
        if not job_title:
            return 32  # Default to 'Other'
            
        job_title_lower = job_title.lower()
        
        # Manufacturing-focused function mapping
        function_keywords = {
            'Skilled Trades': [
                'operator', 'technician', 'maintenance', 'mechanic', 'welder',
                'electrician', 'machinist', 'assembler', 'production worker',
                'skilled trades', 'trades', 'fabricator', 'installer'
            ],
            'Engineering': [
                'mechanical engineer', 'mechanical', 'mech eng', 'electrical engineer',
                'electrical', 'elec eng', 'civil engineer', 'civil',
            ],
            'Quality': [
                'quality', 'qa', 'qc', 'inspector', 'testing', 'quality control',
                'quality assurance', 'quality engineer'
            ],
            'Manufacturing': [
                'manufacturing', 'plant manager', 'production supervisor',
                'production manager', 'plant', 'factory', 'facility manager'
            ],
            'Operations': [
                'operations', 'supply chain', 'logistics', 'planning', 'scheduler',
                'procurement', 'purchasing', 'buyer',
                'project manager', 'program manager', 'coordinator',
            ],
            'Maintenance': [
                'maintenance', 'facilities', 'building', 'hvac', 'utilities',
                'facility maintenance', 'building maintenance'
            ],
            'Security': [
                'security', 'safety', 'environmental', 'ehs', 'health',
                'safety engineer', 'environmental engineer'
            ],
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer',
                'data', 'analyst', 'database', 'system', 'network'
            ],
            'Accounting': ['finance', 'financial', 'accounting', 'accountant', 'controller'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people'],
            'Sales': ['sales', 'account manager', 'business development'],
            'Marketing': ['marketing', 'brand', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance'],
            'Customer Support': ['customer service', 'support', 'customer'],
            'Administrative': ['admin', 'administrative', 'assistant', 'office']
        }
        
        # Try to match keywords
        with self.conn.cursor() as cursor:
            for function_name, keywords in function_keywords.items():
                for keyword in keywords:
                    if keyword in job_title_lower:
                        cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            logger.info(f"  Mapped '{job_title}' to function: {function_name} (ID: {result['id']})")
                            return result['id']
            
            # Default to 'Other' if no match found
            cursor.execute("SELECT id FROM Functions WHERE name = %s", ('Other',))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped '{job_title}' to function: Other (ID: {result['id']})")
                return result['id']
        
        logger.warning(f"  Could not map '{job_title}' to any function")
        return 32  # Fallback ID
    
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
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-plugins')
            chrome_options.add_argument('--window-size=1280,720')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.page_load_strategy = 'eager'
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36')
            
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(15)
            self.driver.set_script_timeout(10)
            
            logger.info("Selenium WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            wait = WebDriverWait(self.driver, timeout)
            
            # Wait for basic page structure
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning(f"  Body tag not found within timeout")
                return ""
            
            # Give time for dynamic content
            time.sleep(2)
            
            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
                
        except TimeoutException:
            logger.warning(f"  Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""
            
        except Exception as e:
            logger.error(f"  Error loading job page: {e}")
            return ""
    
    def extract_job_description(self, html_content: str) -> str:
        """Extract job description from HTML content, limit to body and stop at ASSA ABLOY Group"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, navigation
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Get body content
            body = soup.find('body')
            if not body:
                logger.warning("  No body tag found")
                return html_content
            
            # Get body text
            body_text = body.get_text(separator=' ', strip=True)
            
            # Stop at "We are the ASSA ABLOY Group"
            cutoff_phrase = "We are the ASSA ABLOY Group"
            cutoff_index = body_text.find(cutoff_phrase)
            if cutoff_index != -1:
                body_text = body_text[:cutoff_index].strip()
                logger.info(f"  Cut off description at 'We are the ASSA ABLOY Group'")
            
            if len(body_text) > 100:
                logger.info(f"  Extracted job description: {len(body_text)} characters")
                return body_text
            else:
                logger.warning(f"  No meaningful job description found")
                return html_content
                
        except Exception as e:
            logger.warning(f"Error extracting job description: {e}")
            return html_content
    
    def detect_shifts(self, job_description: str) -> Dict:
        """Detect shift information from job description"""
        desc_lower = job_description.lower()
        
        shifts = {
            'first_shift': False,
            'second_shift': False,
            'third_shift': False
        }
        
        # Check for specific shift mentions
        second_shift_terms = ['second shift', '2nd shift', 'evening shift', 'afternoon shift']
        third_shift_terms = ['third shift', '3rd shift', 'night shift', 'midnight shift', 'graveyard shift']
        
        if any(term in desc_lower for term in second_shift_terms):
            shifts['second_shift'] = True
            logger.info("  Detected second shift")
        elif any(term in desc_lower for term in third_shift_terms):
            shifts['third_shift'] = True
            logger.info("  Detected third shift")
        else:
            shifts['first_shift'] = True
            logger.info("  Defaulted to first shift")
        
        return shifts
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class AmeristarAssaAbloyScraper:
    """Ameristar/Assa Abloy job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'id': 538,  # Ameristar company ID
            'name': 'Ameristar',
            'website': 'https://www.assaabloy.com',
            'jobboard_url': 'https://www.assaabloy.com/career/en/open-positions',
            'api_endpoint': 'https://www.assaabloy.com/rest/api/v1/job-openings.json'
        }
        
        # Set up session headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
            'Referer': 'https://www.assaabloy.com/career/en/open-positions',
            'Accept': '*/*'
        })
    
    def get_all_jobs(self) -> List[Dict]:
        """Get all job listings from Assa Abloy API"""
        try:
            logger.info("Fetching all jobs from Assa Abloy API...")
            
            response = self.session.get(self.company_config['api_endpoint'])
            response.raise_for_status()
            
            data = response.json()
            jobs = data.get('items', [])
            
            logger.info(f"Retrieved {len(jobs)} total jobs from API")
            return jobs
            
        except Exception as e:
            logger.error(f"Error fetching jobs from API: {e}")
            return []
    
    def filter_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter jobs for Tulsa locations"""
        tulsa_jobs = []
        
        for job in jobs:
            locations = job.get('locations', [])
            for location in locations:
                city = location.get('city', '') or ''  # Handle None values
                if 'Tulsa' in city:
                    tulsa_jobs.append(job)
                    logger.info(f"Found Tulsa job: {job.get('title', 'Unknown')} - {city}")
                    break
        
        logger.info(f"Filtered {len(tulsa_jobs)} Tulsa jobs from {len(jobs)} total")
        return tulsa_jobs
    
    def build_job_url(self, job_req_id: int) -> str:
        """Build job detail URL from job requisition ID"""
        return f"https://www.assaabloy.com/career/en/open-positions/job.{job_req_id}"
    
    def parse_date(self, date_string: str) -> Optional[datetime]:
        """Parse ISO date string to datetime"""
        try:
            if date_string:
                # Parse ISO format: "2025-07-12T09:33:49Z"
                return datetime.fromisoformat(date_string.replace('Z', '+00:00')).replace(tzinfo=None)
            return None
        except Exception as e:
            logger.warning(f"Could not parse date '{date_string}': {e}")
            return None
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data['job_title']}{job_data['posting_url']}{job_data.get('job_description', '')}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def scrape_jobs(self, limit_jobs: int = 5) -> Dict:
        """Main scraping method with job limit for testing"""
        stats = {
            'found': 0,
            'added': 0,
            'updated': 0,
            'skipped': 0,
            'errors': []
        }
        
        try:
            company_id = self.company_config['id']
            
            # Step 1: Get all jobs from API
            logger.info("Step 1: Getting all jobs from Assa Abloy API...")
            all_jobs = self.get_all_jobs()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            
            # Step 2: Filter for Tulsa jobs
            logger.info("Step 2: Filtering for Tulsa jobs...")
            tulsa_jobs = self.filter_tulsa_jobs(all_jobs)
            if not tulsa_jobs:
                logger.warning("No Tulsa jobs found")
                return stats
            
            stats['found'] = len(tulsa_jobs)
            
            # Step 3: Limit jobs for testing
            if limit_jobs and len(tulsa_jobs) > limit_jobs:
                tulsa_jobs = tulsa_jobs[:limit_jobs]
                logger.info(f"Step 3: Limited to first {limit_jobs} jobs for testing")
            
            # Step 4: Process each job
            for i, job_api_data in enumerate(tulsa_jobs):
                try:
                    job_title = job_api_data.get('title', 'Unknown')
                    job_req_id = job_api_data.get('jobReqId')
                    logger.info(f"Processing job {i+1}/{len(tulsa_jobs)}: {job_title}")
                    
                    if not job_req_id:
                        logger.warning(f"  No jobReqId found, skipping")
                        stats['skipped'] += 1
                        continue
                    
                    # Build job URL
                    job_url = self.build_job_url(job_req_id)
                    logger.info(f"  Job URL: {job_url}")
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_url)
                    if existing_job_id:
                        stats['updated'] += 1
                        continue
                    
                    # Scrape full job description for new jobs
                    job_html = self.selenium_scraper.get_job_content(job_url)
                    if not job_html or len(job_html.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    job_description = self.selenium_scraper.extract_job_description(job_html)
                    shifts = self.selenium_scraper.detect_shifts(job_description)
                    
                    # Prepare job data
                    job_data = {
                        'job_title': job_title,
                        'posting_url': job_url,
                        'posting_id': str(job_req_id),
                        'job_description': job_description,
                        'date_posted': self.parse_date(job_api_data.get('postStartDate')),
                        'date_closed': self.parse_date(job_api_data.get('applicationDueDate')),
                        'experience_level_name': job_api_data.get('experienceLevel', {}).get('name', ''),
                        'first_shift': shifts['first_shift'],
                        'second_shift': shifts['second_shift'],
                        'third_shift': shifts['third_shift'],
                        'scraping_hash': ''  # Will be set after we have description
                    }
                    
                    # Set scraping hash
                    job_data['scraping_hash'] = self.create_scraping_hash(job_data)
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_api_data.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 5: Mark stale jobs as closed (only if not in test mode)
            if not limit_jobs:
                logger.info("Step 5: Marking stale jobs as closed...")
                self.db.mark_stale_jobs_closed(company_id)
                
                # Step 6: Update company scrape completion
                logger.info("Step 6: Updating company scrape completion...")
                self.db.update_company_scrape_completed(company_id)
            else:
                logger.info("Skipping stale job cleanup and completion update (test mode)")
            
            # Step 7: Log results
            logger.info("Step 7: Logging results...")
            self.db.log_scraping_activity('Assa Abloy API', stats)
            
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
    
    # Set port based on environment, defaulting to 5432 for production
    db_port = os.getenv('DB_PORT', '5432')  # Production default
    # On your dev box, set: set DB_PORT=5433
    db_connection = f"postgresql://postgres:{db_password}@localhost:{db_port}/tulsa_jobs_dev"

    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = AmeristarAssaAbloyScraper(db_manager)
        
        # RSet limit_jobs=5 to limit for testing
        logger.info("Starting Ameristar/Assa Abloy job scraping (TEST MODE - 5 jobs max)...")
        results = scraper.scrape_jobs(limit_jobs=None)
        
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
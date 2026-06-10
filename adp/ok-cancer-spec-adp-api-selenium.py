#!/usr/bin/env python3
"""
ok-cancer-spec-adp-api-selenium-scrap.py
Oklahoma Cancer Specialists ADP Job Board Scraper
Combines API calls with targeted Selenium scraping for job descriptions
"""

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
import json
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ok_cancer_scraper.log', encoding='utf-8'),
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
        """Check if job URL already exists, update timestamps if found"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                SELECT id FROM joblistings
                WHERE posting_url = %s
            """, (job_url,))

            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE joblistings
                    SET updated_at = CURRENT_TIMESTAMP,
                        last_scraped = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (existing['id'],))
                logger.info(f"  Job already exists (ID: {existing['id']}), updated timestamps")
                return existing['id']
            return None
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Try to map job title to function
            function = self._map_job_to_function(job_data['title'])
            
            # Map job type
            job_type_id = self._map_job_type(job_data.get('job_type', ''))
            
            # Insert new job. approved intentionally omitted — DB default is false.
            cursor.execute("""
                INSERT INTO joblistings (
                    company_id, job_title, job_description, posting_url,
                    source_job_board, date_posted, scraping_hash,
                    function, job_type_id, minimum_salary, maximum_salary,
                    pay_frequency, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         (SELECT id FROM jobstatus WHERE name = 'active'))
                RETURNING id
            """, (
                company_id,
                job_data['title'],
                job_data['description'],
                job_data['url'],
                'Oklahoma Cancer Specialists ADP',
                job_data['date_posted'],
                job_data['scraping_hash'],
                function,
                job_type_id,
                job_data.get('minimum_salary'),
                job_data.get('maximum_salary'),
                job_data.get('pay_frequency'),
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
            return job_id
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords (enhanced for healthcare roles)
        function_keywords = {
            'Healthcare Provider': [
                'nurse', 'rn', 'lpn', 'cna', 'registered nurse', 'medical assistant', 
                'physician', 'doctor', 'md', 'np', 'nurse practitioner', 'pa', 
                'physician assistant', 'therapist', 'physical therapist', 'occupational therapist',
                'respiratory therapist', 'pharmacist', 'pharmacy', 'technician', 'medical technician',
                'lab tech', 'radiology', 'ultrasound', 'xray', 'clinical', 'medical', 'registered'
            ],
            'Administration': [
                'admin', 'administrative', 'coordinator', 'assistant', 'office',
                'scheduler', 'scheduling', 'receptionist', 'clerk', 'registration'
            ],
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data', 
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile'
            ],
            'Finance': [
                'finance', 'financial', 'accounting', 'accountant', 'treasury', 
                'controller', 'audit', 'bookkeeping', 'billing', 'revenue cycle'
            ],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
            'Customer Service': [
                'customer service', 'support', 'help desk', 'call center', 'client',
                'patient services', 'patient representative'
            ],
            'Project Management': ['project manager', 'program manager', 'scrum master', 'project coordinator'],
            'Security': ['security', 'safety', 'guard', 'protection'],
            'Research': ['research', 'researcher', 'clinical research', 'study coordinator']
        }
        
        # Try to match keywords
        for function_name, keywords in function_keywords.items():
            for keyword in keywords:
                if keyword in job_title_lower:
                    # Get function ID from database
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                            return result['id']
        
        # Default to 'Other' if no match found
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM functions WHERE name = %s", ('Other',))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped '{job_title}' to function: Other (no specific match)")
                return result['id']
        
        logger.warning(f"  Could not map '{job_title}' to any function")
        return None
    
    def _map_job_type(self, work_level_code: str) -> Optional[int]:
        """Map ADP work level to job_type_id using LIKE matching"""
        if not work_level_code:
            return None
            
        work_level_lower = work_level_code.lower()
        
        # Define job type mappings
        job_type_mappings = {
            'Full Time': ['full time', 'full-time'],
            'Part Time': ['part time', 'part-time'],
            'Contract': ['contract', 'contractor'],
            'Temporary': ['temporary', 'temp'],
            'Internship': ['intern', 'internship'],
            'Seasonal': ['seasonal']
        }
        
        for job_type_name, keywords in job_type_mappings.items():
            for keyword in keywords:
                if keyword in work_level_lower:
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM jobtype WHERE name LIKE %s", (f"%{job_type_name}%",))
                        result = cursor.fetchone()
                        if result:
                            logger.info(f"  Mapped '{work_level_code}' to job type: {job_type_name}")
                            return result['id']
        
        logger.warning(f"  Could not map '{work_level_code}' to any job type")
        return None
    
    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE company
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
                FROM company
                WHERE id = %s
            """, (company_id,))

            company_data = cursor.fetchone()
            if not company_data or not company_data['last_full_scrape_completed']:
                logger.warning(f"No last_full_scrape_completed date found for company {company_id}")
                return

            last_scrape_date = company_data['last_full_scrape_completed']

            # Close jobs that weren't updated in this scrape cycle
            cursor.execute("""
                UPDATE joblistings SET
                    job_status_id = (SELECT id FROM jobstatus WHERE name = 'closed'),
                    date_closed = CURRENT_DATE
                WHERE company_id = %s
                  AND job_status_id != (SELECT id FROM jobstatus WHERE name = 'closed')
                  AND updated_at < %s
            """, (company_id, last_scrape_date))

            closed_count = cursor.rowcount
            if closed_count > 0:
                logger.info(f"Marked {closed_count} stale jobs as closed")
    
    def log_scraping_activity(self, job_board: str, stats: Dict):
        """Log scraping results"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                INSERT INTO scrapinglog (
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
                chrome_options.add_argument('--disable-software-rasterizer')
                chrome_options.add_argument('--disable-gpu-sandbox')
            
            # Performance optimizations
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--disable-gpu-sandbox')
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
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class OKCancerSpecialistsJobScraper:
    """Oklahoma Cancer Specialists ADP job scraper combining API calls with Selenium"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'Oklahoma Cancer Specialists',
            'website': 'https://ocsri.org/',
            'jobboard_url': 'https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=0344be4a-d36c-4b1f-b96f-8354590ef7dc&ccid=19000101_000001&type=JS&lang=en_US',
            'api_endpoint': 'https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions',
            'cid': '0344be4a-d36c-4b1f-b96f-8354590ef7dc',
            'ccId': '19000101_000001'
        }
        
        self.company_id = 911
        
        # Set up session headers for API calls
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept': 'application/json',
            'DNT': '1',
            'Sec-GPC': '1'
        })
    
    def get_job_listings_from_api(self) -> List[Dict]:
        """Get all job listings from Oklahoma Cancer Specialists ADP API"""
        try:
            logger.info("Fetching job listings from ADP API...")
            
            # Generate timestamp
            timestamp = int(time.time() * 1000)
            
            # Build API URL with parameters
            params = {
                'cid': self.company_config['cid'],
                'timeStamp': timestamp,
                'ccId': self.company_config['ccId'],
                'lang': 'en_US',
                'locale': 'en_US',
                '$top': 100  # Get up to 100 jobs
            }
            
            response = self.session.get(
                self.company_config['api_endpoint'],
                params=params,
                headers={
                    'Referer': self.company_config['jobboard_url']
                }
            )
            
            response.raise_for_status()
            data = response.json()
            
            if 'jobRequisitions' not in data:
                logger.warning("No jobRequisitions found in API response")
                return []
            
            jobs = data['jobRequisitions']
            logger.info(f"Retrieved {len(jobs)} jobs from ADP API")
            return jobs
            
        except Exception as e:
            logger.error(f"Error fetching jobs from API: {e}")
            return []
    
    def filter_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter jobs for Tulsa location"""
        filtered = []

        logger.info(f"Filtering {len(jobs)} jobs for Tulsa location...")

        for job in jobs:
            # Check requisitionLocations for Tulsa
            locations = job.get('requisitionLocations', [])
            for location in locations:
                # Check both the shortName and the cityName
                name_code = location.get('nameCode', {})
                short_name = name_code.get('shortName', '').strip()
            
                # Also check the address cityName
                address = location.get('address', {})
                city_name = address.get('cityName', '').strip()
            
                if 'Tulsa' in short_name or 'Tulsa' in city_name:
                    filtered.append(job)
                    logger.info(f"  ✓ Found Tulsa job: {job.get('requisitionTitle', 'Unknown')} at {short_name or city_name}")
                    break

        logger.info(f"Found {len(filtered)} Tulsa jobs")
        return filtered

    def extract_api_job_data(self, job: Dict) -> Dict:
        """Extract structured data from API job response"""
        try:
            # Extract basic job info
            job_data = {
                'title': job.get('requisitionTitle', ''),
                'external_job_id': None,
                'date_posted': None,
                'minimum_salary': None,
                'maximum_salary': None,
                'pay_frequency': None,
                'job_type': job.get('workLevelCode', {}).get('shortName', '')
            }
            
            # Extract ExternalJobID from stringFields
            string_fields = job.get('customFieldGroup', {}).get('stringFields', [])
            for field in string_fields:
                if field.get('nameCode', {}).get('codeValue') == 'ExternalJobID':
                    job_data['external_job_id'] = field.get('stringValue')
                    break
            
            # Parse posting date
            post_date = job.get('postDate')
            if post_date:
                try:
                    job_data['date_posted'] = datetime.fromisoformat(post_date.replace('Z', '+00:00'))
                except:
                    logger.warning(f"Could not parse date: {post_date}")
            
            # Extract salary information
            pay_grade_range = job.get('payGradeRange', {})
            if pay_grade_range:
                min_rate = pay_grade_range.get('minimumRate', {})
                max_rate = pay_grade_range.get('maximumRate', {})
                
                if min_rate and 'amountValue' in min_rate:
                    job_data['minimum_salary'] = min_rate['amountValue']
                
                if max_rate and 'amountValue' in max_rate:
                    job_data['maximum_salary'] = max_rate['amountValue']
            
            # Extract pay frequency (if available)
            # This might be in different locations depending on ADP configuration
            # Add logic here if pay frequency is found in the API response
            
            return job_data
            
        except Exception as e:
            logger.error(f"Error extracting job data: {e}")
            return {}
    
    def build_job_url(self, external_job_id: str) -> str:
        """Build job detail URL using external job ID"""
        base_url = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        params = {
            'cid': self.company_config['cid'],
            'ccId': self.company_config['ccId'],
            'type': 'MP',
            'lang': 'en_US',
            'selectedMenuKey': 'CareerCenter',
            'jobId': external_job_id
        }
        
        param_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return f"{base_url}?{param_string}"
    
    def scrape_job_description(self, external_job_id: str) -> str:
        """Scrape job description from detail page"""
        job_url = self.build_job_url(external_job_id)
        html_content = self.selenium_scraper.get_job_content(job_url)
        
        if not html_content:
            return ""
        
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find body content only
            body = soup.find('body')
            if not body:
                logger.warning("No body tag found in job page")
                return ""
            
            # Remove unwanted elements
            for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Get text content
            description = body.get_text(strip=True)
            logger.info(f"  Extracted job description: {len(description)} characters")
            return description
            
        except Exception as e:
            logger.warning(f"Error extracting job description: {e}")
            return html_content
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data['title']}{job_data['url']}{job_data.get('description', '')}"
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
            # Step 1: Use hardcoded company ID
            logger.info(f"Step 1: Using company ID: {self.company_id}")
            
            # Step 2: Get job listings from API
            logger.info("Step 2: Getting job listings from API...")
            all_jobs = self.get_job_listings_from_api()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            
            # Step 3: Filter for Tulsa jobs
            logger.info("Step 3: Filtering for Tulsa jobs...")
            tulsa_jobs = self.filter_tulsa_jobs(all_jobs)
            stats['found'] = len(tulsa_jobs)
            
            if len(tulsa_jobs) == 0:
                logger.warning("No Tulsa jobs found")
                return stats
            
            # Step 4: Process each Tulsa job
            for i, job in enumerate(tulsa_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(tulsa_jobs)}: {job.get('requisitionTitle', 'Unknown')}")
                    
                    # Extract API data
                    api_data = self.extract_api_job_data(job)
                    if not api_data.get('external_job_id'):
                        logger.warning("  No external job ID found, skipping")
                        stats['skipped'] += 1
                        continue
                    
                    # Build job URL
                    job_url = self.build_job_url(api_data['external_job_id'])
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_url)
                    if existing_job_id:
                        stats['updated'] += 1
                        continue
                    
                    # Scrape job description for new jobs only
                    job_description = self.scrape_job_description(api_data['external_job_id'])
                    if not job_description or len(job_description.strip()) < 50:
                        logger.warning("  Failed to get meaningful job description")
                        stats['skipped'] += 1
                        continue
                    
                    # Prepare complete job data
                    job_data = {
                        'title': api_data['title'],
                        'url': job_url,
                        'description': job_description,
                        'date_posted': api_data['date_posted'],
                        'minimum_salary': api_data['minimum_salary'],
                        'maximum_salary': api_data['maximum_salary'],
                        'pay_frequency': api_data['pay_frequency'],
                        'job_type': api_data['job_type'],
                        'scraping_hash': self.create_scraping_hash({
                            'title': api_data['title'],
                            'url': job_url,
                            'description': job_description
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('requisitionTitle', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 5: Mark stale jobs as closed
            logger.info("Step 5: Marking stale jobs as closed...")
            self.db.mark_stale_jobs_closed(self.company_id)
            
            # Step 6: Update company scrape completion
            logger.info("Step 6: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)

            
            # Step 7: Log results
            logger.info("Step 7: Logging results...")
            self.db.log_scraping_activity('Oklahoma Cancer Specialists ADP', stats)
            
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
    db_password = os.getenv('POSTGRES_PASSWORD')
    if not db_password:
        logger.error("Please set POSTGRES_PASSWORD environment variable")
        logger.error("Example: set POSTGRES_PASSWORD=your_password")
        return 1

    db_host = os.getenv('POSTGRES_HOST', 'localhost')
    db_port = os.getenv('POSTGRES_PORT', '5432')
    db_name = os.getenv('POSTGRES_DB', 'tulsajobspot')
    db_user = os.getenv('POSTGRES_USER', 'tulsajobspot')
    db_connection = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = OKCancerSpecialistsJobScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Oklahoma Cancer Specialists ADP job scraping...")
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
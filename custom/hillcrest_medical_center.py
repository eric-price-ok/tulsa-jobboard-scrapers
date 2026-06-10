#!/usr/bin/env python3
"""
Complete Hillcrest Medical Center Job Scraper with Selenium
Handles JavaScript-heavy Angular application and extracts job details
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
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hillcrest_scraper.log', encoding='utf-8'),
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
    
    def get_or_create_company(self, company_data: Dict) -> int:
        """Get existing company or create new one, return company ID"""
        with self.conn.cursor() as cursor:
            # Check if company exists
            cursor.execute(
                "SELECT id FROM Company WHERE common_name = %s",
                (company_data['name'],)
            )
            result = cursor.fetchone()
            
            if result:
                return result['id']
            
            # Create new company
            cursor.execute("""
                INSERT INTO Company (common_name, website, jobboard, is_tulsa_based, approved)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_data['name'],
                company_data['website'],
                company_data['jobboard_url'],
                True,
                True
            ))
            
            result = cursor.fetchone()
            company_id = result['id']
            logger.info(f"Created new company: {company_data['name']} (ID: {company_id})")
            return company_id
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> tuple[int, bool]:
        """Store or update job listing, return (job_id, is_new)"""
        with self.conn.cursor() as cursor:
            # Check for existing job by URL
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s
            """, (job_data['url'],))
            
            existing = cursor.fetchone()
            
            # Try to map job title to function
            function = self._map_job_to_function(job_data['title'])
            
            if existing:
                # Update existing job - mark as still active
                cursor.execute("""
                    UPDATE JobListings SET
                        job_title = %s,
                        job_description = %s,
                        posting_url = %s,
                        scraping_hash = %s,
                        function = %s,
                        last_scraped = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id
                """, (
                    job_data['title'],
                    job_data.get('description', ''),
                    job_data['url'],
                    job_data['scraping_hash'],
                    function,
                    existing['id']
                ))
                result = cursor.fetchone()
                logger.info(f"Updated existing job: {job_data['title']} (ID: {existing['id']})")
                return result['id'], False
            else:
                # Insert new job
                cursor.execute("""
                    INSERT INTO JobListings (
                        company_id, job_title, job_description, posting_url, 
                        source_job_board, scraping_hash, 
                        function, approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data.get('description', ''),
                    job_data['url'],
                    'Hillcrest Medical Center',
                    job_data['scraping_hash'],
                    function,
                    True
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id, True
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using hospital-specific keywords"""
        job_title_lower = job_title.lower()
        
        # Define hospital-specific function mapping keywords
        function_keywords = {
            'Healthcare Provider': [
                'nurse', 'rn', 'lpn', 'cna', 'doctor', 'physician', 'surgeon', 'therapist',
                'medical assistant', 'paramedic', 'emt', 'practitioner', 'specialist',
                'anesthesia', 'respiratory', 'physical therapy', 'occupational therapy',
                'speech therapy', 'social worker', 'chaplain', 'dietitian', 'nutritionist'
            ],
            'Clinical Support': [
                'lab', 'laboratory', 'phlebotomist', 'radiology', 'imaging', 'ultrasound',
                'mri', 'ct', 'x-ray', 'pharmacy', 'pharmacist', 'technician', 'tech',
                'medical records', 'coding', 'health information', 'sterile processing',
                'surgical tech', 'operating room', 'or tech', 'cardiology', 'echo'
            ],
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'data', 
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'epic',
                'cerner', 'health information systems'
            ],
            'Administration': [
                'admin', 'administrative', 'coordinator', 'assistant', 'office', 'clerk',
                'secretary', 'receptionist', 'registration', 'admissions', 'discharge',
                'case manager', 'utilization review', 'quality', 'compliance', 'risk'
            ],
            'Finance': [
                'finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 
                'audit', 'billing', 'revenue cycle', 'patient accounts', 'collections'
            ],
            'Human Resources': [
                'hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits',
                'payroll', 'compensation', 'training', 'development'
            ],
            'Facilities': [
                'maintenance', 'facility', 'facilities', 'engineering', 'hvac', 'plumbing',
                'electrical', 'biomedical', 'environmental services', 'housekeeping',
                'custodial', 'groundskeeper', 'plant operations'
            ],
            'Food Service': [
                'food', 'kitchen', 'cook', 'chef', 'dietary', 'nutrition', 'cafeteria',
                'food service', 'culinary'
            ],
            'Security': [
                'security', 'safety', 'guard', 'protection', 'emergency management'
            ],
            'Transportation': [
                'transport', 'driver', 'ambulance', 'patient transport', 'courier'
            ]
        }
        
        # Try to match keywords
        for function_name, keywords in function_keywords.items():
            for keyword in keywords:
                if keyword in job_title_lower:
                    # Get function ID from database
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                            return result['id']
        
        # Default to 'Other' if no match found
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM Functions WHERE name = %s", ('Other',))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped '{job_title}' to function: Other (no specific match)")
                return result['id']
        
        logger.warning(f"  Could not map '{job_title}' to any function")
        return None
    
    def mark_old_jobs_closed(self, company_id: int):
        """Mark jobs as closed if not updated today"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE JobListings SET 
                    job_status_id = 6
                WHERE company_id = %s 
                AND DATE(updated_at) < CURRENT_DATE
                AND job_status_id = (SELECT id FROM JobStatus WHERE name = 'Active')
            """, (company_id,))
            
            closed_count = cursor.rowcount
            if closed_count > 0:
                logger.info(f"Marked {closed_count} old jobs as closed")
    
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
            chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            self.driver.implicitly_wait(10)
            self.driver.set_page_load_timeout(30)
            
            logger.info("Selenium WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_listings(self, base_url: str) -> List[Dict]:
        """Get all job listings from Hillcrest job board"""
        all_jobs = []
        page = 1
        
        while True:
            url = f"{base_url}?stretch=10&stretchUnit=MILES&location=Tulsa,%20OK&page={page}&tags8=Hillcrest%20Medical%20Center"
            
            logger.info(f"Fetching jobs from page {page}")
            
            try:
                # Load page
                self.driver.get(url)
                
                # Wait for job listings to load
                wait = WebDriverWait(self.driver, 15)
                try:
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.job-title-link")))
                except TimeoutException:
                    logger.warning(f"No job listings found on page {page}")
                    break
                
                # Give extra time for content to load
                time.sleep(2)
                
                # Parse the page
                soup = BeautifulSoup(self.driver.page_source, 'html.parser')
                job_links = soup.find_all('a', class_='job-title-link')
                
                if not job_links:
                    logger.info(f"No more jobs found on page {page}")
                    break
                
                logger.info(f"Found {len(job_links)} jobs on page {page}")
                
                # Extract job data from each link
                for link in job_links:
                    job_data = self._extract_job_from_link(link, base_url)
                    if job_data:
                        all_jobs.append(job_data)
                
                page += 1
                time.sleep(1)  # Be respectful between pages
                
            except Exception as e:
                logger.error(f"Error fetching page {page}: {e}")
                break
        
        return all_jobs
    
    def _extract_job_from_link(self, job_link, base_url: str) -> Optional[Dict]:
        """Extract job data from a job link element"""
        try:
            # Extract job title
            title_span = job_link.find('span', {'itemprop': 'title'})
            if not title_span:
                return None
            
            job_title = title_span.get_text(strip=True)
            
            # Extract URL
            relative_url = job_link.get('href')
            if not relative_url:
                return None
            
            job_url = 'https://jobs.ardenthealth.com' + relative_url
            
            # Extract posting ID from container
            container = job_link
            posting_id = ''
            
            for _ in range(5):  # Look up to 5 levels up
                container = container.find_parent()
                if not container:
                    break
                
                job_id_text = container.find(string=lambda text: text and 'Job ID:' in text)
                if job_id_text:
                    parent = job_id_text.parent
                    if parent:
                        job_id_span = parent.find('span')
                        if job_id_span:
                            posting_id = job_id_span.get_text(strip=True)
                            break
            
            return {
                'title': job_title,
                'url': job_url,
                'posting_id': posting_id
            }
            
        except Exception as e:
            logger.warning(f"Error extracting job data: {e}")
            return None
    
    def get_job_content(self, job_url: str) -> str:
        """Load job detail page and extract clean content"""
        try:
            logger.info(f"  Loading job detail page...")
            self.driver.get(job_url)
            
            # Wait for page to load
            wait = WebDriverWait(self.driver, 15)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning(f"  Timeout loading job detail page")
                return ""
            
            # Give time for dynamic content
            time.sleep(2)
            
            # Get page source and clean it
            page_source = self.driver.page_source
            return self._clean_job_content(page_source)
            
        except Exception as e:
            logger.error(f"  Error loading job detail page: {e}")
            return ""
    
    def _clean_job_content(self, html_content: str) -> str:
        """Extract and clean job content from HTML"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, and other unwanted elements
            for tag in soup.find_all(['script', 'style', 'noscript', 'meta', 'link', 'head']):
                tag.decompose()
            
            # Get body content only
            body = soup.find('body')
            if not body:
                return ""
            
            # Remove navigation, header, footer elements
            for tag in body.find_all(['nav', 'header', 'footer', 'aside']):
                tag.decompose()
            
            # Remove elements with navigation/menu classes
            for tag in body.find_all(class_=re.compile(r'nav|menu|header|footer|sidebar', re.I)):
                tag.decompose()
            
            # Get cleaned text content
            cleaned_content = str(body)
            
            # Basic cleanup
            cleaned_content = re.sub(r'\s+', ' ', cleaned_content)  # Normalize whitespace
            cleaned_content = cleaned_content.strip()
            
            logger.info(f"  Extracted clean content: {len(cleaned_content)} characters")
            return cleaned_content
            
        except Exception as e:
            logger.warning(f"Error cleaning job content: {e}")
            return html_content
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

class HillcrestScraperWithSelenium:
    """Hillcrest scraper that uses Selenium for JavaScript-heavy job pages"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        self.company_config = {
            'name': 'Hillcrest Medical Center',
            'website': 'https://hillcrest.com',
            'jobboard_url': 'https://jobs.ardenthealth.com/hillcrest-medical-center/jobs',
            'base_url': 'https://jobs.ardenthealth.com/hillcrest-medical-center/jobs'
        }
    
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
            # Step 1: Get company ID
            logger.info("Step 1: Getting/creating company...")
            company_id = self.db.get_or_create_company(self.company_config)
            logger.info(f"✓ Company ID: {company_id}")
            
            # Step 2: Get all job listings
            logger.info("Step 2: Getting job listings...")
            all_jobs = self.selenium_scraper.get_job_listings(self.company_config['base_url'])
            stats['found'] = len(all_jobs)
            
            if len(all_jobs) == 0:
                logger.warning("No jobs found")
                return stats
            
            logger.info(f"✓ Found {len(all_jobs)} total jobs")
            
            # Step 3: Process each job
            for i, job in enumerate(all_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(all_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Check if job already exists (by URL)
                    job_exists = False
                    with self.db.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM JobListings WHERE posting_url = %s", (job['url'],))
                        if cursor.fetchone():
                            job_exists = True
                    
                    # Get job content if new job or if we want to update description
                    job_description = ""
                    if not job_exists:
                        job_description = self.selenium_scraper.get_job_content(job['url'])
                        if not job_description:
                            logger.warning(f"  Failed to get job content, storing basic info")
                    
                    # Prepare job data for database
                    job_data = {
                        'title': job.get('title', ''),
                        'url': job['url'],
                        'description': job_description,
                        'scraping_hash': self.create_scraping_hash({
                            'title': job.get('title', ''),
                            'url': job['url'],
                            'description': job_description
                        })
                    }
                    
                    # Store job in database
                    job_id, is_new = self.db.store_job_listing(job_data, company_id)
                    
                    if is_new:
                        stats['added'] += 1
                    else:
                        stats['updated'] += 1
                    
                    logger.info(f"  ✓ {'Added' if is_new else 'Updated'} job with ID: {job_id}")
                    
                    # Be respectful with timing
                    time.sleep(0.5)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark old jobs as closed
            logger.info("Step 4: Marking old jobs as closed...")
            self.db.mark_old_jobs_closed(company_id)
            
            # Step 5: Log results
            logger.info("Step 5: Logging results...")
            self.db.log_scraping_activity('Hillcrest Medical Center', stats)
            
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
        scraper = HillcrestScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting Hillcrest Medical Center job scraping with Selenium...")
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
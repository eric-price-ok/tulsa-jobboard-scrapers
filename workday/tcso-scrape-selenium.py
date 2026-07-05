#!/usr/bin/env python3
"""
TCSO-Workday-API-Selenium.py
Tulsa County Sheriff's Office (TCSO) Job Scraper
Uses Workday API with Selenium for job detail extraction
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
        logging.FileHandler('tcso_scraper.log', encoding='utf-8'),
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
                INSERT INTO Company (common_name, website, jobboard, company_type, approved)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_data['name'],
                company_data['website'],
                company_data['jobboard_url'],
                6,
                True
            ))
            
            result = cursor.fetchone()
            company_id = result['id']
            logger.info(f"Created new company: {company_data['name']} (ID: {company_id})")
            return company_id
    
    def store_job_listing(self, job_data: Dict, company_id: int, extracted_fields: Dict = None) -> int:
        """Store or update job listing with extracted fields, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Check for existing job by URL and title+company
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s 
                OR (job_title = %s AND company_id = %s)
            """, (job_data['url'], job_data['title'], company_id))
            
            existing = cursor.fetchone()
            
            # Try to map job title to function (first try extracted category, then title)
            function = None
            if extracted_fields and extracted_fields.get('category'):
                function = self._map_category_to_function(extracted_fields['category'])
            
            if not function:
                function = self._map_job_to_function(job_data['title'])
            
            # Use extracted fields if available
            date_posted = job_data.get('date_posted')
            if extracted_fields and extracted_fields.get('date_posted'):
                date_posted = extracted_fields['date_posted']
            
            posting_id = extracted_fields.get('posting_id') if extracted_fields else None
            date_closed = extracted_fields.get('date_closed') if extracted_fields else None
            minimum_salary = extracted_fields.get('minimum_salary') if extracted_fields else None
            maximum_salary = extracted_fields.get('maximum_salary') if extracted_fields else None
            pay_frequency = extracted_fields.get('pay_frequency').lower() if extracted_fields and extracted_fields.get('pay_frequency') else None
            
            # Handle job type mapping
            job_type_id = None
            if extracted_fields and extracted_fields.get('job_type'):
                job_type_id = self._map_job_type_to_id(extracted_fields['job_type'])
            
            # Use pay amount as max salary if no salary range found
            if not maximum_salary and extracted_fields and extracted_fields.get('pay_amount'):
                maximum_salary = extracted_fields['pay_amount']
            
            if existing:
                # Update existing job
                cursor.execute("""
                    UPDATE JobListings SET
                        job_title = %s,
                        job_description = %s,
                        posting_url = %s,
                        date_posted = %s,
                        scraping_hash = %s,
                        Function = %s,
                        posting_id = %s,
                        date_closed = %s,
                        minimum_salary = %s,
                        maximum_salary = %s,
                        pay_frequency = %s,
                        job_type_id = %s,
                        last_scraped = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id
                """, (
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    date_posted,
                    job_data['scraping_hash'],
                    function,
                    posting_id,
                    date_closed,
                    minimum_salary,
                    maximum_salary,
                    pay_frequency,
                    job_type_id,
                    existing['id']
                ))
                result = cursor.fetchone()
                logger.info(f"Updated existing job: {job_data['title']} (ID: {existing['id']})")
                return result['id']
            else:
                # Insert new job
                cursor.execute("""
                    INSERT INTO JobListings (
                        company_id, job_title, job_description, posting_url, 
                        source_job_board, Date_Posted, scraping_hash, 
                        Function, Approved, Job_Status_Id, Posting_ID, Date_Closed,
                        Minimum_Salary, Maximum_Salary, pay_frequency, job_type_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'),
                             %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'TCSO Workday',
                    date_posted,
                    job_data['scraping_hash'],
                    function,
                    True,
                    posting_id,
                    date_closed,
                    minimum_salary,
                    maximum_salary,
                    pay_frequency,
                    job_type_id
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id
    
    def _map_job_type_to_id(self, job_type_text: str) -> Optional[int]:
        """Map job type text to JobType table ID"""
        with self.conn.cursor() as cursor:
            job_type_lower = job_type_text.lower().strip()
            
            # Direct mapping logic
            if 'full time' in job_type_lower or 'full-time' in job_type_lower:
                job_type_name = 'Full Time'
            elif 'part time' in job_type_lower or 'part-time' in job_type_lower:
                job_type_name = 'Part Time'
            elif 'contract to hire' in job_type_lower:
                job_type_name = 'Contract to Hire'
            elif 'contract' in job_type_lower or '1099' in job_type_lower:
                job_type_name = 'Contract (1099)'
            else:
                logger.warning(f"  Could not map job type '{job_type_text}' to any JobType")
                return None
            
            # Get the ID from database
            cursor.execute("SELECT id FROM JobType WHERE name = %s", (job_type_name,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped job type '{job_type_text}' to: {job_type_name} (ID: {result['id']})")
                return result['id']
            
            logger.warning(f"  JobType '{job_type_name}' not found in database")
            return None
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords - TCSO specific mappings"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords (enhanced for sheriff's office roles)
        function_keywords = {
            'Security': [
                'deputy', 'sheriff', 'officer', 'detention', 'correctional', 'security',
                'patrol', 'investigator', 'detective', 'sergeant', 'lieutenant', 'captain',
                'jailer', 'jail', 'corrections', 'enforcement', 'police'
            ],
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data', 
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'cyber'
            ],
            'Administrative': [
                'admin', 'administrative', 'coordinator', 'assistant', 'office', 'clerk',
                'secretary', 'records', 'filing', 'data entry', 'receptionist'
            ],
            'Accounting': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit', 'payroll'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
            'Operations': ['project manager', 'program manager', 'coordinator'],
            'Customer Support': ['customer service', 'support', 'help desk', 'call center', 'client', 'public'],
            'Healthcare': ['nurse', 'doctor', 'medical', 'healthcare', 'clinical', 'physician', 'emt', 'paramedic'],
            'Skilled Trades': ['maintenance', 'mechanic', 'technician', 'facilities', 'custodial', 'grounds'],
            'Marketing': ['communications', 'dispatch', 'dispatcher', 'radio', '911', 'emergency']
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
    
    def _map_category_to_function(self, category: str) -> Optional[int]:
        """Map TCSO category to function ID"""
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM Functions WHERE name = %s", (category,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped category '{category}' to function: {category}")
                return result['id']
            
            # Try partial matches for common TCSO categories
            category_lower = category.lower()
            category_mappings = {
                'law enforcement': 'Security',
                'corrections': 'Security',
                'detention': 'Security',
                'administrative': 'Administration',
                'information technology': 'Information Technology',
                'finance': 'Finance',
                'human resources': 'Human Resources',
                'legal': 'Legal',
                'medical': 'Healthcare Provider',
                'maintenance': 'Skilled Labor',
                'communications': 'Communications',
                'dispatch': 'Communications'
            }
            
            for key, function_name in category_mappings.items():
                if key in category_lower:
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        logger.info(f"  Mapped category '{category}' to function: {function_name}")
                        return result['id']
            
            logger.warning(f"  Could not map category '{category}' to any function")
            return None
    
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
    
    def mark_old_jobs_closed(self, company_id: int, scrape_start_time: datetime):
        """Mark jobs as closed if they weren't seen in the current scrape"""
        with self.conn.cursor() as cursor:
            # Close jobs that weren't updated during this scrape session
            cursor.execute("""
                UPDATE JobListings SET 
                    Job_Status_Id = 6,
                    Date_Closed = CURRENT_DATE
                WHERE company_id = %s 
                AND (last_scraped IS NULL OR last_scraped < %s)
                AND Job_Status_Id = (SELECT id FROM JobStatus WHERE name = 'Active')
                AND Date_Closed IS NULL
            """, (company_id, scrape_start_time))
            
            closed_count = cursor.rowcount
            if closed_count > 0:
                logger.info(f"Marked {closed_count} old jobs as closed (not found in current scrape)")
    
    def mark_scrape_completed(self, company_id: int):
        """Mark that a full scrape has been completed for this company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (company_id,))
            
            logger.info(f"Marked scrape as completed for company ID {company_id}")

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
            chrome_options.add_argument('--window-size=1280,720')
            
            # Disable logging and error messages
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_argument('--disable-gpu-logging')
            chrome_options.add_argument('--disable-extensions-http-throttling')
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
            
            logger.info("Optimized Selenium WebDriver initialized")
            
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
            time.sleep(1.5)
            
            # Quick check for job content
            page_text = ""
            try:
                body_element = self.driver.find_element(By.TAG_NAME, "body")
                page_text = body_element.text
            except:
                pass
            
            if len(page_text.strip()) > 200:
                logger.info(f"  Page content loaded: {len(page_text)} characters")
            else:
                logger.warning(f"  Limited content found: {len(page_text)} characters")
            
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

class TCSKOScraperWithSelenium:
    """TCSO scraper that uses Workday API + Selenium for job details"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        # TCSO-specific configuration
        self.company_config = {
            'name': 'Tulsa County Sheriff\'s Office',
            'website': 'https://www.tulsasheriff.org',
            'jobboard_url': 'https://tulsacounty.wd1.myworkdayjobs.com/TCSO/',
            'api_endpoint': 'https://tulsacounty.wd1.myworkdayjobs.com/wday/cxs/tulsacounty/TCSO/jobs'
        }
        
        # Set up session headers for API calls
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })
    
    def establish_session(self) -> bool:
        """Establish session with Workday site"""
        try:
            logger.info("Establishing session with TCSO careers page...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from TCSO Workday API"""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None
        
        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                
                body = {
                    "appliedFacets": {},
                    "limit": limit,
                    "offset": offset,
                    "searchText": ""
                }
                
                response = self.session.post(
                    self.company_config['api_endpoint'],
                    json=body,
                    headers={
                        'Referer': self.company_config['jobboard_url'],
                        'Origin': 'https://tulsacounty.wd1.myworkdayjobs.com',
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    }
                )
                
                response.raise_for_status()
                data = response.json()
                
                if 'jobPostings' not in data:
                    break
                
                if total_results is None:
                    total_results = data.get('total', 0)
                    logger.info(f"Total jobs available: {total_results}")
                
                batch_jobs = data['jobPostings']
                all_jobs.extend(batch_jobs)
                
                logger.info(f"Retrieved {len(batch_jobs)} jobs. Total so far: {len(all_jobs)}")
                
                offset += limit
                if offset >= total_results or len(batch_jobs) == 0:
                    break
                
                time.sleep(0.5)
                
            except Exception as e:
                logger.error(f"Error fetching jobs: {e}")
                break
        
        return all_jobs
    
    def parse_posted_date(self, posted_text: str) -> Optional[datetime]:
        """Parse 'Posted X Days Ago' text to actual date"""
        if not posted_text:
            return None
        
        try:
            clean_text = re.sub(r'^Posted\s+', '', posted_text, flags=re.IGNORECASE)
            clean_text = re.sub(r'\s*\+?\s*Days?\s+Ago$', '', clean_text, flags=re.IGNORECASE)
            clean_text = re.sub(r'\s*\+?\s*Day\s+Ago$', '', clean_text, flags=re.IGNORECASE)
            
            days_ago = int(clean_text)
            return datetime.now() - timedelta(days=days_ago)
            
        except (ValueError, TypeError):
            logger.warning(f"Could not parse posted date: {posted_text}")
            return None
    
    def extract_job_content(self, html_content: str) -> tuple[str, Dict]:
        """Extract job content and parse specific fields from HTML"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Initialize extracted fields
            extracted_fields = {
                'date_posted': None,
                'posting_id': None,
                'category': None,
                'date_closed': None,
                'minimum_salary': None,
                'maximum_salary': None,
                'job_type': None,
                'pay_frequency': None,
                'pay_amount': None
            }
            
            # Extract date posted: <span class="fw-bold">Posted</span><br/>6/26/2025
            try:
                posted_elements = soup.find_all('span', class_='fw-bold', string='Posted')
                for elem in posted_elements:
                    if elem.next_sibling and elem.next_sibling.name == 'br':
                        date_text = elem.next_sibling.next_sibling
                        if date_text and isinstance(date_text, str):
                            date_text = date_text.strip()
                            if date_text:
                                extracted_fields['date_posted'] = self.parse_date(date_text)
                                logger.info(f"  Extracted date posted: {date_text}")
                                break
            except Exception as e:
                logger.warning(f"  Could not extract date posted: {e}")
            
            # Extract posting ID: <span class="fw-bold">ID</span><br/>R9600
            try:
                id_elements = soup.find_all('span', class_='fw-bold', string='ID')
                for elem in id_elements:
                    if elem.next_sibling and elem.next_sibling.name == 'br':
                        id_text = elem.next_sibling.next_sibling
                        if id_text and isinstance(id_text, str):
                            id_text = id_text.strip()
                            if id_text:
                                extracted_fields['posting_id'] = id_text
                                logger.info(f"  Extracted posting ID: {id_text}")
                                break
            except Exception as e:
                logger.warning(f"  Could not extract posting ID: {e}")
            
            # Extract category: <span class="fw-bold">Category</span><br/>Law Enforcement
            try:
                category_elements = soup.find_all('span', class_='fw-bold', string='Category')
                for elem in category_elements:
                    if elem.next_sibling and elem.next_sibling.name == 'br':
                        category_text = elem.next_sibling.next_sibling
                        if category_text and isinstance(category_text, str):
                            category_text = category_text.strip()
                            if category_text:
                                extracted_fields['category'] = category_text
                                logger.info(f"  Extracted category: {category_text}")
                                break
            except Exception as e:
                logger.warning(f"  Could not extract category: {e}")
            
            # Extract date closed: <b>Job Posting End Date</b></p>07-07-2025
            try:
                end_date_elements = soup.find_all('b', string='Job Posting End Date')
                for elem in end_date_elements:
                    # Look for the date after the closing </p> tag
                    next_elem = elem.find_next_sibling()
                    if next_elem and next_elem.string:
                        date_text = next_elem.string.strip()
                        if date_text:
                            extracted_fields['date_closed'] = self.parse_date(date_text)
                            logger.info(f"  Extracted date closed: {date_text}")
                            break
                    # Alternative: look for text immediately following
                    parent = elem.parent
                    if parent:
                        text_after = parent.get_text()
                        # Extract date pattern after "Job Posting End Date"
                        date_match = re.search(r'Job Posting End Date.*?(\d{1,2}-\d{1,2}-\d{4})', text_after)
                        if date_match:
                            date_text = date_match.group(1)
                            extracted_fields['date_closed'] = self.parse_date(date_text)
                            logger.info(f"  Extracted date closed: {date_text}")
                            break
            except Exception as e:
                logger.warning(f"  Could not extract date closed: {e}")
            
            # Extract job type: <span class="emphasis-3">Full Time / Part Time</span></b></h1>Full time
            try:
                job_type_elements = soup.find_all('span', class_='emphasis-3', string=re.compile(r'Full Time.*Part Time', re.IGNORECASE))
                for elem in job_type_elements:
                    # Look for the text after the </h1> tag
                    parent = elem.parent
                    while parent and parent.name != 'h1':
                        parent = parent.parent
                    
                    if parent:
                        # Get the next sibling after the h1 tag
                        next_elem = parent.next_sibling
                        if next_elem and isinstance(next_elem, str):
                            job_type_text = next_elem.strip()
                        else:
                            # Try to get text content after the h1
                            text_after = parent.find_next(string=True)
                            if text_after:
                                job_type_text = text_after.strip()
                            else:
                                continue
                        
                        if job_type_text:
                            extracted_fields['job_type'] = job_type_text
                            logger.info(f"  Extracted job type: {job_type_text}")
                            break
            except Exception as e:
                logger.warning(f"  Could not extract job type: {e}")
            
            # Extract pay frequency: <span class="emphasis-3">Pay Frequency</span></b></h1><p>Monthly</p>
            try:
                pay_freq_elements = soup.find_all('span', class_='emphasis-3', string='Pay Frequency')
                for elem in pay_freq_elements:
                    # Look for the <p> tag after the h1
                    parent = elem.parent
                    while parent and parent.name != 'h1':
                        parent = parent.parent
                    
                    if parent:
                        # Find the next <p> tag
                        next_p = parent.find_next('p')
                        if next_p and next_p.string:
                            pay_freq_text = next_p.string.strip()
                            if pay_freq_text:
                                extracted_fields['pay_frequency'] = pay_freq_text
                                logger.info(f"  Extracted pay frequency: {pay_freq_text}")
                                break
            except Exception as e:
                logger.warning(f"  Could not extract pay frequency: {e}")
            
            # Extract pay amount: <span class="emphasis-3">Pay</span></u></b></h1>$15.92
            try:
                pay_elements = soup.find_all('span', class_='emphasis-3', string='Pay')
                for elem in pay_elements:
                    # Look for the text after the </h1> tag
                    parent = elem.parent
                    while parent and parent.name != 'h1':
                        parent = parent.parent
                    
                    if parent:
                        # Get the text immediately following the h1
                        next_elem = parent.next_sibling
                        if next_elem and isinstance(next_elem, str):
                            pay_text = next_elem.strip()
                        else:
                            # Try to get text content after the h1
                            text_after = parent.find_next(string=True)
                            if text_after:
                                pay_text = text_after.strip()
                            else:
                                continue
                        
                        # Extract dollar amount: $15.92
                        pay_match = re.search(r'\$?([\d,]+\.?\d*)', pay_text)
                        if pay_match:
                            pay_amount = pay_match.group(1).replace(',', '')
                            try:
                                extracted_fields['pay_amount'] = float(pay_amount)
                                logger.info(f"  Extracted pay amount: ${pay_amount}")
                                break
                            except ValueError:
                                logger.warning(f"  Could not parse pay amount: {pay_amount}")
            except Exception as e:
                logger.warning(f"  Could not extract pay amount: {e}")
            # Extract salary range: $40,000.00-60,000.00 USD (if present in standard format)
            try:
                # Look for "Compensation Range:" or "Salary Range:" text
                comp_elements = soup.find_all(string=re.compile(r'(Compensation|Salary)\s+Range:', re.IGNORECASE))
                for elem in comp_elements:
                    # Get the parent and look for salary pattern
                    parent = elem.parent if hasattr(elem, 'parent') else None
                    if parent:
                        # Look in the same element and next siblings for salary range
                        text_content = parent.get_text()
                        # Pattern: $XXX,XXX.XX-XXX,XXX.XX USD
                        salary_match = re.search(r'\$?([\d,]+\.?\d*)\s*-\s*([\d,]+\.?\d*)\s*USD?', text_content, re.IGNORECASE)
                        if salary_match:
                            min_sal = salary_match.group(1).replace(',', '')
                            max_sal = salary_match.group(2).replace(',', '')
                            try:
                                extracted_fields['minimum_salary'] = float(min_sal)
                                extracted_fields['maximum_salary'] = float(max_sal)
                                logger.info(f"  Extracted salary range: ${min_sal} - ${max_sal}")
                                break
                            except ValueError:
                                logger.warning(f"  Could not parse salary values: {min_sal}, {max_sal}")
            except Exception as e:
                logger.warning(f"  Could not extract salary range: {e}")
            
            # Remove scripts, styles, navigation for main content extraction
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Try to find job-specific content
            job_selectors = [
                '[data-automation-id="jobPostingDescription"]',
                '[data-automation-id="jobDescription"]',
                '.jobPostingDescription',
                '.job-description',
                '.job-details',
                '[role="main"]',
                'main'
            ]
            
            main_content = ""
            for selector in job_selectors:
                content = soup.select_one(selector)
                if content and len(content.get_text(strip=True)) > 100:
                    logger.info(f"  Extracted content using selector: {selector}")
                    main_content = str(content)
                    break
            
            # Fallback: return body content if job-specific selectors don't work
            if not main_content:
                body = soup.find('body')
                if body:
                    # Remove common non-content elements
                    for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                        tag.decompose()
                    
                    body_text = body.get_text(strip=True)
                    if len(body_text) > 100:
                        logger.info(f"  Using body content: {len(body_text)} characters")
                        main_content = str(body)
                    else:
                        main_content = html_content
                else:
                    main_content = html_content
            
            return main_content, extracted_fields
            
        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return html_content, {}
    
    def parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse date string in various formats"""
        import re
        from datetime import datetime
        
        try:
            # Remove extra whitespace
            date_str = date_str.strip()
            
            # Try different date formats
            date_formats = [
                '%m/%d/%Y',    # 6/26/2025
                '%m-%d-%Y',    # 6-26-2025
                '%d-%d-%Y',    # 07-07-2025
                '%Y-%m-%d',    # 2025-06-26
                '%m/%d/%y',    # 6/26/25
                '%m-%d-%y'     # 6-26-25
            ]
            
            for fmt in date_formats:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            
            logger.warning(f"Could not parse date: {date_str}")
            return None
            
        except Exception as e:
            logger.warning(f"Error parsing date '{date_str}': {e}")
            return None
    
    def download_job_details(self, job_url: str) -> tuple[str, Dict]:
        """Download job details using Selenium and return content + extracted fields"""
        html_content = self.selenium_scraper.get_job_content(job_url)
        if html_content:
            return self.extract_job_content(html_content)
        return "", {}
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data['title']}{job_data['url']}{job_data.get('description', '')}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        # Record when this scrape session starts
        scrape_start_time = datetime.now()
        
        stats = {
            'found': 0,
            'added': 0,
            'updated': 0,
            'skipped': 0,
            'errors': []
        }
        
        try:
            # Step 1: Establish session
            logger.info("Step 1: Establishing session...")
            if not self.establish_session():
                raise Exception("Failed to establish session")
            
            # Step 2: Get company ID
            logger.info("Step 2: Getting/creating company...")
            company_id = self.db.get_or_create_company(self.company_config)
            logger.info(f"✓ Company ID: {company_id}")
            
            # Step 3: Get job listings from API
            logger.info("Step 3: Getting job listings from TCSO Workday API...")
            all_jobs = self.get_job_listings()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            logger.info(f"✓ Retrieved {len(all_jobs)} jobs from API")
            
            # Step 4: All jobs are relevant (no location filtering needed for TCSO)
            stats['found'] = len(all_jobs)
            logger.info(f"Step 4: Processing all {len(all_jobs)} TCSO jobs")
            
            # Step 5: Process each job with Selenium
            for i, job in enumerate(all_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(all_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Build job URL
                    external_path = job.get('externalPath', '')
                    if not external_path:
                        logger.warning(f"  No externalPath found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://tulsacounty.wd1.myworkdayjobs.com/TCSO{external_path}"
                    logger.info(f"  Job URL: {job_url}")
                    
                    # Download job details with Selenium
                    job_html, extracted_fields = self.download_job_details(job_url)
                    if not job_html or len(job_html.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    logger.info(f"  Downloaded job content: {len(job_html)} chars")
                    
                    # Log extracted fields
                    if extracted_fields:
                        for field, value in extracted_fields.items():
                            if value:
                                logger.info(f"  {field}: {value}")
                    
                    # Prepare job data for database
                    job_data = {
                        'title': job.get('title', ''),
                        'url': job_url,
                        'description': job_html,
                        'date_posted': self.parse_posted_date(job.get('postedOn', '')),
                        'scraping_hash': self.create_scraping_hash({
                            'title': job.get('title', ''),
                            'url': job_url,
                            'description': job_html
                        })
                    }
                    
                    # Store job in database with extracted fields
                    job_id = self.db.store_job_listing(job_data, company_id, extracted_fields)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(2.0)  # 2 second delay between job page scrapes
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 6: Mark scrape as completed and close old jobs
            logger.info("Step 6: Marking scrape as completed...")
            self.db.mark_scrape_completed(company_id)
            
            logger.info("Step 7: Marking old jobs as closed...")
            self.db.mark_old_jobs_closed(company_id, scrape_start_time)
            
            # Step 8: Log results
            logger.info("Step 8: Logging results...")
            self.db.log_scraping_activity('TCSO Workday', stats)
            
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
        scraper = TCSKOScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting TCSO job scraping with Selenium...")
        results = scraper.scrape_jobs()
        
        # Print summary
        logger.info("=== SCRAPING SUMMARY ===")
        logger.info(f"Jobs found: {results['found']}")
        logger.info(f"Jobs added/updated: {results['added']}")
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
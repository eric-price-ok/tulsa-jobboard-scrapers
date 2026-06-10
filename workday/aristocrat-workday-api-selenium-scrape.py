#!/usr/bin/env python3
"""
aristocrat-workday-scrape.py
Aristocrat Gaming Oklahoma Job Scraper
Uses Aristocrat's Workday API filtered for Tulsa and Bartlesville
"""

from utils.date_utilities import parse_relative_date, format_date_for_db, get_cutoff_date
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
        logging.FileHandler('aristocrat_scraper.log', encoding='utf-8'),
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
                INSERT INTO Company (common_name, website, jobboard, approved, company_type)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_data['name'],
                company_data['website'],
                company_data['jobboard_url'],
                True,
                4
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
            function_id = None
            if extracted_fields and extracted_fields.get('category'):
                function_id = self._map_category_to_function(extracted_fields['category'])
            
            if not function_id:
                function_id = self._map_job_to_function(job_data['title'])
            
            # Use extracted fields if available
            date_posted = job_data.get('date_posted')
            if extracted_fields and extracted_fields.get('date_posted'):
                date_posted = extracted_fields['date_posted']
            
            posting_id = extracted_fields.get('posting_id') if extracted_fields else None
            date_closed = extracted_fields.get('date_closed') if extracted_fields else None
            minimum_salary = extracted_fields.get('minimum_salary') if extracted_fields else None
            maximum_salary = extracted_fields.get('maximum_salary') if extracted_fields else None
            office_location_id = extracted_fields.get('office_location_id') if extracted_fields else None
            
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
                        office_location_id = %s,
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
                    function_id,
                    posting_id,
                    date_closed,
                    minimum_salary,
                    maximum_salary,
                    office_location_id,
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
                        source_job_board, date_posted, scraping_hash, 
                        Function, Approved, job_status_id, posting_id, date_closed,
                        minimum_salary, maximum_salary, office_location_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'),
                             %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Aristocrat Oklahoma',
                    date_posted,
                    job_data['scraping_hash'],
                    function_id,
                    True,
                    posting_id,
                    date_closed,
                    minimum_salary,
                    maximum_salary,
                    office_location_id
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id
    
    def _map_category_to_function(self, category: str) -> Optional[int]:
        """Map Aristocrat category to function ID"""
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM Functions WHERE name = %s", (category,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped category '{category}' to function: {category}")
                return result['id']
            
            # Try partial matches for common Aristocrat categories
            category_lower = category.lower()
            category_mappings = {
                'gaming': 'Manufacturing',  # Gaming machines manufacturing
                'information technology': 'Information Technology',
                'engineering': 'Engineering, Mechanical',  # Default to mechanical for gaming machines
                'software': 'Information Technology',
                'finance': 'Finance',
                'human resources': 'Human Resources',
                'legal': 'Legal',
                'operations': 'Project Management',
                'manufacturing': 'Manufacturing',
                'quality': 'Quality',
                'sales': 'Sales',
                'customer': 'Customer Service',
                'administrative': 'Administration',
                'marketing': 'Marketing'
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
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords - Aristocrat Gaming specific mappings"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords (enhanced for gaming/casino company roles)
        function_keywords = {
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data', 
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'cyber'
            ],
            'Engineering, Mechanical': [
                'mechanical', 'mech eng', 'mechanical engineer', 'gaming machine', 
                'slot machine', 'hardware engineer', 'product engineer'
            ],
            'Engineering, Electrical': [
                'electrical', 'elec eng', 'electrical engineer', 'electronics', 
                'embedded systems', 'firmware', 'hardware'
            ],
            'Manufacturing': [
                'manufacturing', 'production', 'assembly', 'quality control', 'factory',
                'machine operator', 'production supervisor', 'plant', 'gaming machine',
                'slot manufacturing', 'cabinet'
            ],
            'Sales': [
                'sales', 'account manager', 'business development', 'territory', 'casino sales',
                'gaming sales', 'field sales', 'regional sales', 'account executive'
            ],
            'Customer Service': [
                'customer service', 'support', 'technical support', 'field service',
                'service technician', 'customer success', 'help desk', 'client support'
            ],
            'Project Management': [
                'project manager', 'program manager', 'product manager', 'operations',
                'business analyst', 'process improvement', 'operations manager'
            ],
            'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Marketing': [
                'marketing', 'brand', 'digital marketing', 'content', 'social media', 
                'communications', 'product marketing', 'brand manager'
            ],
            'Legal': [
                'legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 
                'regulatory', 'gaming compliance', 'regulatory affairs'
            ],
            'Quality': [
                'quality', 'qa', 'qc', 'testing', 'inspector', 'assurance', 
                'quality engineer', 'test engineer', 'quality control'
            ],
            'Skilled Labor': [
                'technician', 'maintenance', 'mechanic', 'installer', 'field technician',
                'service tech', 'repair', 'assembly technician'
            ],
            'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
            'Security': ['security', 'safety', 'guard', 'protection']
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
                    job_status_id = 6,
                    date_closed = CURRENT_DATE
                WHERE company_id = %s 
                AND (last_scraped IS NULL OR last_scraped < %s)
                AND job_status_id = (SELECT id FROM JobStatus WHERE name = 'Active')
                AND date_closed IS NULL
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

class AristocratOklahomaScraper:
    """Aristocrat Gaming scraper using Workday API filtered for Oklahoma locations"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        # Aristocrat Gaming-specific configuration
        self.company_config = {
            'name': 'Aristocrat Gaming',
            'website': 'https://aristocrat.com',
            'jobboard_url': 'https://aristocrat.wd3.myworkdayjobs.com/AristocratExternalCareersSite?locationRegionStateProvince=205ef21c556342268847466c48ec5a8c',
            'api_endpoint': 'https://aristocrat.wd3.myworkdayjobs.com/wday/cxs/aristocrat/AristocratExternalCareersSite/jobs',
            'location_filters': ['Tulsa', 'Bartlesville']
        }
        
        # Set up session for API calls
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })
    
    def establish_session(self) -> bool:
        """Establish session with Workday site"""
        try:
            logger.info("Establishing session with Aristocrat careers page...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from Aristocrat Workday API"""
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
                        'Origin': 'https://aristocrat.wd3.myworkdayjobs.com',
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
    
    def filter_oklahoma_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter jobs for Tulsa and Bartlesville area"""
        filtered = []
        
        logger.info(f"Starting filter with {len(jobs)} total jobs")
        
        for i, job in enumerate(jobs):
            location_text = job.get('locationsText', '')
            logger.info(f"Job {i+1}: '{job.get('title', 'Unknown')}' - Location: '{location_text}'")
            
            for location in self.company_config['location_filters']:  # ['Tulsa', 'Bartlesville']
                if location.lower() in location_text.lower():
                    filtered.append(job)
                    logger.info(f"  ? MATCHED on '{location}'")
                    break
            else:
                logger.info(f"  ? No match found")
        
        logger.info(f"Filtered {len(filtered)} jobs for Oklahoma from {len(jobs)} total")
        return filtered
    
    def _map_remote_type_to_office_location(self, remote_type: str) -> Optional[int]:
        """Map remote type string to office_location_id from database"""
        try:
            with self.db.conn.cursor() as cursor:
                # Try exact match first (case insensitive)
                cursor.execute(
                    "SELECT id FROM OfficeLocations WHERE LOWER(name) = LOWER(%s)",
                    (remote_type,)
                )
                result = cursor.fetchone()
                
                if result:
                    return result['id']
                
                # Try partial matches for common variations
                remote_type_lower = remote_type.lower()
                
                # Map common variations
                mappings = {
                    'hybrid': ['hybrid', 'hybrid work'],
                    'remote': ['remote', 'fully remote', 'work from home', 'wfh'],
                    'onsite': ['onsite', 'on-site', 'office', 'in-person', 'on site'],
                    'flexible': ['flexible', 'flex']
                }
                
                for key, variations in mappings.items():
                    if any(variation in remote_type_lower for variation in variations):
                        cursor.execute(
                            "SELECT id FROM OfficeLocations WHERE LOWER(name) LIKE %s",
                            (f'%{key}%',)
                        )
                        result = cursor.fetchone()
                        if result:
                            return result['id']
                
                logger.warning(f"Could not map remote type '{remote_type}' to any office location")
                return None
                
        except Exception as e:
            logger.error(f"Error mapping remote type to office location: {e}")
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
                'office_location_id': None
            }
            
            # Extract posting ID from subtitle section
            try:
                subtitle_ul = soup.find('ul', {'data-automation-id': 'subtitle', 'class': 'css-14a0imc'})
                if subtitle_ul:
                    li_elements = subtitle_ul.find_all('li', class_='css-h2nt8k')
                    for li in li_elements:
                        text = li.get_text(strip=True)
                        # Look for posting ID pattern: R followed by digits
                        if re.match(r'^R\d+$', text):
                            extracted_fields['posting_id'] = text
                            logger.info(f"  Extracted posting ID: {text}")
                            break
                
                # Alternative: look for any element containing R-number pattern
                if not extracted_fields['posting_id']:
                    r_number_elements = soup.find_all(string=re.compile(r'R\d{6,}'))
                    for element in r_number_elements:
                        text = element.strip()
                        if re.match(r'^R\d+$', text):
                            extracted_fields['posting_id'] = text
                            logger.info(f"  Extracted posting ID (alternative): {text}")
                            break

            except Exception as e:
                logger.warning(f"  Could not extract posting ID: {e}")

            # Extract remote/office type and map to office_location_id
            try:
                # Look for dt element with "remote type" text
                remote_type_dt = soup.find('dt', class_='css-y8qsrx', string=re.compile(r'remote\s+type', re.IGNORECASE))
                if remote_type_dt:
                    # Find the corresponding dd element (next sibling)
                    remote_type_dd = remote_type_dt.find_next_sibling('dd', class_='css-129m7dg')
                    if remote_type_dd:
                        remote_type_text = remote_type_dd.get_text(strip=True)
                        office_location_id = self._map_remote_type_to_office_location(remote_type_text)
                        if office_location_id:
                            extracted_fields['office_location_id'] = office_location_id
                            logger.info(f"  Extracted remote type: {remote_type_text} -> office_location_id: {office_location_id}")
                        else:
                            logger.info(f"  Found remote type '{remote_type_text}' but could not map to office_location_id")

            except Exception as e:
                logger.warning(f"  Could not extract remote type: {e}")
            
            # Look for other Workday metadata
            try:
                metadata_selectors = [
                    '[data-automation-id*="date"]',
                    '[data-automation-id*="posted"]',
                    '[data-automation-id*="category"]'
                ]
                
                for selector in metadata_selectors:
                    elements = soup.select(selector)
                    for elem in elements:
                        text = elem.get_text(strip=True)
                        if text and len(text) > 0:
                            logger.info(f"  Found metadata: {text}")

            except Exception as e:
                logger.warning(f"  Could not extract other Workday metadata: {e}")
            
            # Remove scripts, styles, navigation for main content extraction
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Try to find job-specific content (Workday selectors)
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
            logger.info(f"? Company ID: {company_id}")
            
            # Step 3: Get job listings
            logger.info("Step 3: Getting job listings from API...")
            all_jobs = self.get_job_listings()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            logger.info(f"? Retrieved {len(all_jobs)} jobs from API")
            
            # Step 4: Filter for Oklahoma area (Tulsa & Bartlesville)
            logger.info("Step 4: Filtering for Oklahoma area...")
            oklahoma_jobs = self.filter_oklahoma_jobs(all_jobs)
            stats['found'] = len(oklahoma_jobs)
            
            if len(oklahoma_jobs) == 0:
                logger.warning("No jobs found after filtering")
                return stats
            
            # Step 5: Process each job with Selenium
            for i, job in enumerate(oklahoma_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(oklahoma_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Build job URL
                    external_path = job.get('externalPath', '')
                    if not external_path:
                        logger.warning(f"  No externalPath found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://aristocrat.wd3.myworkdayjobs.com/AristocratExternalCareersSite{external_path}"
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
                        'date_posted': parse_relative_date(job.get('postedOn', '')),
                        'scraping_hash': self.create_scraping_hash({
                            'title': job.get('title', ''),
                            'url': job_url,
                            'description': job_html
                        })
                    }
                    
                    # Store job in database with extracted fields
                    job_id = self.db.store_job_listing(job_data, company_id, extracted_fields)
                    logger.info(f"  ? Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(0.5)  # Small delay between job page scrapes
                    
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
            self.db.log_scraping_activity('Aristocrat Oklahoma', stats)
            
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
    
    db_connection = f"postgresql://postgres:{db_password}@192.168.250.13:5432/tulsa_jobs"
    
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = AristocratOklahomaScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Aristocrat Oklahoma job scraping...")
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
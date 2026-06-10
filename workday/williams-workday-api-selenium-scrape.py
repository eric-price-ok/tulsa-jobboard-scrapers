#!/usr/bin/env python3
"""
williams-workday-api-selenium-scrape.py
Williams Workday Job Scraper
Uses two-stage filtering and Selenium for JavaScript-heavy job pages
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
        logging.FileHandler('williams_scraper.log', encoding='utf-8'),
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
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store or update job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Check for existing job by URL and title+company
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s 
                OR (job_title = %s AND company_id = %s)
            """, (job_data['url'], job_data['title'], company_id))
            
            existing = cursor.fetchone()
            
            # Try to map job title to function
            function = self._map_job_to_function(job_data['title'])
            
            if existing:
                # Update existing job
                cursor.execute("""
                    UPDATE JobListings SET
                        job_title = %s,
                        job_description = %s,
                        posting_url = %s,
                        date_posted = %s,
                        scraping_hash = %s,
                        function = %s,
                        last_scraped = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id
                """, (
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    job_data['date_posted'],
                    job_data['scraping_hash'],
                    function,
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
                        function, approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Williams Workday',
                    job_data['date_posted'],
                    job_data['scraping_hash'],
                    function,
                    True
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords - Williams/Oil & Gas specific mappings"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords (enhanced for Williams oil & gas midstream company roles)
        function_keywords = {
            'Information Technology': [
                'software', 'developer', 'programmer', 'data', 
                'analyst', 'database', 'system', 'network', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'scrum', 'agile', 'cyber'
            ],
            'Engineering, Mechanical': [
                'mechanical', 'mech eng', 'mechanical engineer', 'pipeline', 'compressor',
                'facilities', 'plant engineer', 'process engineer', 'compression',
                'gas processing', 'midstream', 'facility engineer', 'rotating equipment',
                'turbine', 'pump', 'valve'
            ],
            'Engineering, Electrical': [
                'electrical', 'elec eng', 'electrical engineer', 'instrumentation', 'controls',
                'scada', 'automation', 'control systems', 'plc', 'dcs'
            ],
            'Engineering, Civil': [
                'civil', 'civil engineer', 'structural', 'geotechnical'
            ],
            'Engineering, Other': [
                'chemical engineer', 'petroleum engineer', 'process engineer',
                'reliability engineer', 'corrosion engineer'
            ],
            'Project Management': [
                'operations', 'ops', 'plant', 'facility', 'gas plant', 'processing',
                'dispatch', 'pipeline operations', 'gas transportation', 'midstream operations',
                'project manager', 'program manager', 'scrum master', 'project coordinator'
            ],
            'Skilled Labor': [
                'technician', 'maintenance', 'mechanic', 'welder', 'electrician', 
                'apprentice', 'journeyman', 'crew', 'field', 'pipeline technician',
                'compression technician', 'field operations', 'operator', 'control room'
            ],
            'Transportation/Logistics': [
                'pipeline operations', 'gas transportation', 'logistics', 'supply chain',
                'transportation', 'shipping', 'distribution'
            ],
            'Finance': [
                'finance', 'financial', 'accounting', 'accountant', 'treasury', 
                'controller', 'audit', 'tax', 'budget'
            ],
            'Human Resources': [
                'hr', 'human resources', 'recruiter', 'talent', 'people', 
                'benefit', 'compensation', 'payroll'
            ],
            'Sales': [
                'sales', 'account manager', 'business development', 'bd', 
                'revenue', 'customer', 'commercial'
            ],
            'Marketing': [
                'marketing', 'brand', 'digital marketing', 'content', 
                'social media', 'communications', 'public relations'
            ],
            'Legal': [
                'legal', 'attorney', 'lawyer', 'counsel', 'compliance', 
                'contract', 'regulatory', 'paralegal'
            ],
            'Customer Service': [
                'customer service', 'support', 'help desk', 'call center', 'client'
            ],
            'Administration': [
                'admin', 'administrative', 'coordinator', 'assistant', 'office', 'clerk'
            ],
            'Quality': [
                'quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'
            ],
            'Security': [
                'security', 'safety', 'guard', 'protection', 'hse', 'health safety'
            ],
            'Purchasing': [
                'purchasing', 'procurement', 'buyer', 'sourcing', 'vendor'
            ],
            'Research': [
                'research', 'r&d', 'development', 'innovation', 'scientist'
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
    
    def mark_old_jobs_closed(self, company_id: int, cutoff_date: datetime):
        """Mark jobs as closed if not seen in recent scrapes"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE JobListings SET 
                    job_status_id = (SELECT id FROM JobStatus WHERE name = 'Expired'),
                    date_closed = CURRENT_DATE
                WHERE company_id = %s 
                AND last_scraped < %s 
                AND job_status_id = (SELECT id FROM JobStatus WHERE name = 'Active')
            """, (company_id, cutoff_date))
            
            closed_count = cursor.rowcount
            if closed_count > 0:
                logger.info(f"Marked {closed_count} old jobs as closed")

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
            
            # Wait for basic page structure - much faster than waiting for specific content
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning(f"  Body tag not found within timeout")
                return ""
            
            # Give minimal time for dynamic content - reduced from 3 seconds
            time.sleep(1.5)
            
            # Quick check for job content without extensive searching
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

class WilliamsScraperWithSelenium:
    """Williams scraper with two-stage filtering and Selenium for job pages"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'Williams',
            'website': 'https://www.williams.com',
            'jobboard_url': 'https://williams.wd5.myworkdayjobs.com/External/',
            'api_endpoint': 'https://williams.wd5.myworkdayjobs.com/wday/cxs/williams/External/jobs',
            'company_id': 1172  # Pre-existing company record
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
            logger.info("Establishing session with Williams careers page...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from Williams Workday API using pagination"""
        all_jobs = []
        limit = 20  # Known working limit
        offset = 0
        total_results = None
        
        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                
                # Use simple working configuration
                body = {
                    "limit": limit,
                    "offset": offset
                }
                
                response = self.session.post(
                    self.company_config['api_endpoint'],
                    json=body,
                    headers={
                        'Referer': self.company_config['jobboard_url'],
                        'Origin': 'https://williams.wd5.myworkdayjobs.com',
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
    
    def job_exists_in_database(self, job_url: str) -> bool:
        """Check if job already exists in database by URL"""
        with self.db.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM JobListings WHERE posting_url = %s", (job_url,))
            result = cursor.fetchone()
            return result is not None
    
    def update_existing_job(self, job_url: str) -> bool:
        """Update last_scraped and updated_at for existing job"""
        with self.db.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE JobListings SET
                    last_scraped = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE posting_url = %s
                RETURNING id
            """, (job_url,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  ? Updated existing job (ID: {result['id']}) - skipped scraping")
                return True
            return False
    
    def filter_potential_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Stage 1 Filter: Get jobs that might be Tulsa-related (Tulsa or multi-location)"""
        filtered = []
        
        logger.info(f"Stage 1 Filter: Starting with {len(jobs)} total jobs")
        
        for i, job in enumerate(jobs):
            location_text = job.get('locationsText', '')
            title = job.get('title', 'Unknown')
            
            # Check for Tulsa or multi-location indicators
            is_potential_tulsa = False
            reason = ""
            
            if 'tulsa' in location_text.lower():
                is_potential_tulsa = True
                reason = "Contains 'Tulsa'"
            elif 'locations' in location_text.lower():
                is_potential_tulsa = True
                reason = "Multi-location job (may include Tulsa)"
            else:
                reason = "No Tulsa or multi-location indicator"
            
            if is_potential_tulsa:
                filtered.append(job)
                logger.info(f"? STAGE 1 ACCEPT: {title}")
                logger.info(f"    Location: {location_text}")
                logger.info(f"    Reason: {reason}")
            else:
                logger.info(f"? STAGE 1 REJECT: {title}")
                logger.info(f"    Location: {location_text}")
                logger.info(f"    Reason: {reason}")
        
        logger.info(f"Stage 1 Filter Results: {len(filtered)} jobs passed (will be checked)")
        return filtered
    
    def validate_tulsa_job(self, job_html: str, job_title: str) -> bool:
        """Stage 2 Filter: Check if job page actually mentions Tulsa headquarters"""
        try:
            soup = BeautifulSoup(job_html, 'html.parser')
            
            # Look for the locations div specifically
            locations_div = soup.find('div', {'data-automation-id': 'locations'})
            
            if locations_div:
                # Get all the location dd elements
                location_elements = locations_div.find_all('dd', class_='css-129m7dg')
                locations = [dd.get_text(strip=True) for dd in location_elements]
                
                logger.info(f"  Found locations: {locations}")
                
                # Check if any location contains Tulsa
                for location in locations:
                    if 'tulsa' in location.lower():
                        logger.info(f"  ? STAGE 2 ACCEPT: Found Tulsa location '{location}'")
                        return True
                
                logger.info(f"  ? STAGE 2 REJECT: No Tulsa location found in {locations}")
                return False
            else:
                # Fallback: search entire page for Tulsa indicators
                page_text = soup.get_text()
                tulsa_indicators = ["OK Tulsa - Headquarters", "tulsa", "oklahoma"]
                
                for indicator in tulsa_indicators:
                    if indicator.lower() in page_text.lower():
                        logger.info(f"  ? STAGE 2 ACCEPT: Found '{indicator}' in page content (fallback)")
                        return True
                
                logger.info(f"  ? STAGE 2 REJECT: No locations div found and no Tulsa indicators in page")
                return False
                
        except Exception as e:
            logger.warning(f"  Error validating Tulsa job: {e}")
            return False
    
    def extract_job_content(self, html_content: str) -> str:
        """Extract job content from HTML"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, navigation
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()
            
            # Try to find job-specific content
            job_selectors = [
                '[data-automation-id="jobPostingDescription"]',
                '[data-automation-id="jobDescription"]',
                '.jobPostingDescription',
                '.job-description',
                '[role="main"]',
                'main'
            ]
            
            for selector in job_selectors:
                content = soup.select_one(selector)
                if content and len(content.get_text(strip=True)) > 100:
                    logger.info(f"  Extracted content using selector: {selector}")
                    return str(content)
            
            # Fallback: return body content if job-specific selectors don't work
            body = soup.find('body')
            if body:
                # Remove common non-content elements
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside']):
                    tag.decompose()
                
                body_text = body.get_text(strip=True)
                if len(body_text) > 100:
                    logger.info(f"  Using body content: {len(body_text)} characters")
                    return str(body)
            
            logger.warning(f"  No meaningful content found")
            return html_content
            
        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return html_content
    
    def download_job_details(self, job_url: str) -> str:
        """Download job details using Selenium"""
        html_content = self.selenium_scraper.get_job_content(job_url)
        if html_content:
            return self.extract_job_content(html_content)
        return ""
    
    def create_scraping_hash(self, job_data: Dict) -> str:
        """Create hash for duplicate detection"""
        content = f"{job_data['title']}{job_data['url']}{job_data.get('description', '')}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def scrape_jobs(self) -> Dict:
        """Main scraping method with two-stage filtering"""
        stats = {
            'found': 0,
            'stage1_accepted': 0,
            'stage1_rejected': 0,
            'stage2_accepted': 0,
            'stage2_rejected': 0,
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
            
            # Step 2: Use hardcoded company ID
            logger.info("Step 2: Using Williams company ID...")
            company_id = self.company_config['company_id']
            logger.info(f"? Company ID: {company_id}")
            
            # Step 3: Get all job listings from API
            logger.info("Step 3: Getting all job listings from Williams Workday API...")
            all_jobs = self.get_job_listings()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            logger.info(f"? Retrieved {len(all_jobs)} total jobs from API")
            
            stats['found'] = len(all_jobs)
            
            # Step 4: Stage 1 Filter - Potential Tulsa jobs
            logger.info("Step 4: Stage 1 Filter - Identifying potential Tulsa jobs...")
            potential_tulsa_jobs = self.filter_potential_tulsa_jobs(all_jobs)
            stats['stage1_accepted'] = len(potential_tulsa_jobs)
            stats['stage1_rejected'] = len(all_jobs) - len(potential_tulsa_jobs)
            
            if len(potential_tulsa_jobs) == 0:
                logger.warning("No potential Tulsa jobs found after Stage 1 filter")
                return stats
            
            logger.info(f"? Stage 1 Filter: {len(potential_tulsa_jobs)} jobs will be checked")
            
            # Step 5: Process each potential job with database check and Selenium scraping
            logger.info("Step 5: Processing jobs with database check and validation...")
            for i, job in enumerate(potential_tulsa_jobs):
                try:
                    title = job.get('title', 'Unknown')
                    location = job.get('locationsText', 'Unknown')
                    
                    logger.info(f"Processing job {i+1}/{len(potential_tulsa_jobs)}: {title}")
                    logger.info(f"  Location: {location}")
                    
                    # Build job URL
                    external_path = job.get('externalPath', '')
                    if not external_path:
                        logger.warning(f"  No externalPath found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://williams.wd5.myworkdayjobs.com/External{external_path}"
                    logger.info(f"  Job URL: {job_url}")
                    
                    # Check if job already exists in database
                    if self.job_exists_in_database(job_url):
                        logger.info(f"  ?? Job already exists in database - updating timestamps")
                        if self.update_existing_job(job_url):
                            stats['updated'] += 1
                        else:
                            stats['skipped'] += 1
                        continue
                    
                    # Job doesn't exist - scrape it
                    logger.info(f"  ?? New job - scraping details...")
                    
                    # Download job details with Selenium
                    job_html = self.download_job_details(job_url)
                    if not job_html or len(job_html.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    logger.info(f"  Downloaded job content: {len(job_html)} chars")
                    
                    # Stage 2 Filter: Validate this is actually a Tulsa job
                    is_tulsa_job = self.validate_tulsa_job(job_html, title)
                    
                    if not is_tulsa_job:
                        stats['stage2_rejected'] += 1
                        logger.info(f"  ? Job rejected by Stage 2 filter")
                        continue
                    
                    stats['stage2_accepted'] += 1
                    logger.info(f"  ? Job confirmed as Tulsa position")
                    
                    # Prepare job data for database
                    job_data = {
                        'title': title,
                        'url': job_url,
                        'description': job_html,
                        'date_posted': parse_relative_date(job.get('postedOn', '')),
                        'scraping_hash': self.create_scraping_hash({
                            'title': title,
                            'url': job_url,
                            'description': job_html
                        })
                    }
                    
                    # Store new job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ? Stored new job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(0.5)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 6: Mark old jobs as closed
            logger.info("Step 6: Marking old jobs as closed...")
            cutoff_date = get_cutoff_date(7)
            self.db.mark_old_jobs_closed(company_id, cutoff_date)
            
            # Step 7: Log results
            logger.info("Step 7: Logging results...")
            self.db.log_scraping_activity('Williams Workday', stats)
            
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
        scraper = WilliamsScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting Williams job scraping with two-stage filtering...")
        results = scraper.scrape_jobs()
        
        # Print detailed summary
        logger.info("=== WILLIAMS SCRAPING SUMMARY ===")
        logger.info(f"Total jobs found: {results['found']}")
        logger.info(f"Stage 1 Filter (Tulsa or multi-location):")
        logger.info(f"  +- Accepted: {results['stage1_accepted']} jobs")
        logger.info(f"  +- Rejected: {results['stage1_rejected']} jobs")
        logger.info(f"Stage 2 Filter (actual Tulsa validation):")
        logger.info(f"  +- Confirmed Tulsa jobs: {results['stage2_accepted']} jobs")
        logger.info(f"  +- Non-Tulsa jobs: {results['stage2_rejected']} jobs")
        logger.info(f"Database operations:")
        logger.info(f"  +- New jobs added: {results['added']}")
        logger.info(f"  +- Existing jobs updated: {results['updated']}")
        logger.info(f"  +- Jobs skipped: {results['skipped']}")
        logger.info(f"Errors: {len(results['errors'])}")
        
        # Calculate efficiency
        total_processed = results['stage1_accepted']
        total_scraped = results['stage2_accepted'] + results['stage2_rejected']
        existing_jobs = results['updated']
        
        if total_processed > 0:
            scraping_efficiency = ((total_processed - existing_jobs) / total_processed) * 100 if total_processed > existing_jobs else 0
            logger.info(f"Scraping efficiency: {scraping_efficiency:.1f}% of jobs required scraping (rest were existing)")
            
        if total_scraped > 0:
            filter_efficiency = (results['stage2_accepted'] / total_scraped) * 100
            logger.info(f"Filter efficiency: {filter_efficiency:.1f}% of scraped jobs were actual Tulsa positions")
        
        if results['errors']:
            logger.error("Errors encountered:")
            for error in results['errors']:
                logger.error(f"  - {error}")
        
        # Final result
        new_jobs = results['added']
        updated_jobs = results['updated']
        total_tulsa_jobs = results['stage2_accepted']
        
        if new_jobs > 0:
            logger.info(f"?? SUCCESS: Found and stored {new_jobs} new Williams Tulsa jobs!")
        if updated_jobs > 0:
            logger.info(f"?? Updated {updated_jobs} existing Williams jobs")
        if total_tulsa_jobs == 0 and new_jobs == 0:
            logger.info("??  No new Tulsa jobs found - all current jobs already in database")
        
    except Exception as e:
        logger.error(f"Script failed: {e}")
        return 1
    finally:
        if scraper:
            scraper.cleanup()
    
    return 0

if __name__ == "__main__":
    exit(main())
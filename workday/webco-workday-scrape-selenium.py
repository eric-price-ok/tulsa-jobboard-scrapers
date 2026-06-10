#!/usr/bin/env python3
"""
webco-workday-scrape.py
Webco Workday Job Scraper
Uses Webco's Workday API with location filtering for Tulsa area
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
        logging.FileHandler('webco_scraper.log', encoding='utf-8'),
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
                INSERT INTO Company (common_name, website, jobboard, approved,company_type)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                company_data['name'],
                company_data['website'],
                company_data['jobboard_url'],
                True,
                5
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
            
            # Try to map job title to function
            function_id = self._map_job_to_function(job_data['title'])
            
            # Use extracted fields if available
            date_posted = job_data.get('date_posted')
            if extracted_fields and extracted_fields.get('date_posted'):
                date_posted = extracted_fields['date_posted']
            
            posting_id = extracted_fields.get('posting_id') if extracted_fields else None
            date_closed = extracted_fields.get('date_closed') if extracted_fields else None
            minimum_salary = extracted_fields.get('minimum_salary') if extracted_fields else None
            maximum_salary = extracted_fields.get('maximum_salary') if extracted_fields else None
            
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
                        minimum_salary, maximum_salary
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'),
                             %s, %s, %s, %s)
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Webco Workday',
                    date_posted,
                    job_data['scraping_hash'],
                    function_id,
                    True,
                    posting_id,
                    date_closed,
                    minimum_salary,
                    maximum_salary
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using keywords - improved for manufacturing roles"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords with priority order (most specific first)
        function_keywords = {
            'Machinist': ['machinist', 'cnc', 'lathe', 'mill operator'],
            'Manufacturing': [
                'manufacturing', 'production', 'assembly', 'factory', 'plant operator',
                'entry level manufacturing', 'manufacturing associate', 'production associate'
            ],
            'Engineering, Mechanical': [
                'mechanical engineer', 'mech engineer', 'industrial engineer', 
                'process engineer', 'design engineer'
            ],
            'Engineering, Electrical': ['electrical engineer', 'elec engineer'],
            'Engineering, Civil': ['civil engineer'],
            'Skilled Labor': [
                'welder', 'electrician', 'maintenance', 'technician', 'operator',
                'skilled labor', 'trades', 'journeyman', 'apprentice'
            ],
            'Information Technology': [
                'software', 'developer', 'programmer', 'it ', 'data analyst', 
                'database', 'system admin', 'network', 'cyber security', 'devops', 
                'cloud', 'web', 'mobile', 'qa tester'
            ],
            'Project Management': [
                'project manager', 'program manager', 'scrum master', 
                'project coordinator', 'operations manager'
            ],
            'Quality': ['quality', 'qa', 'qc', 'inspector', 'quality control'],
            'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue', 'customer'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 'regulatory'],
            'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client'],
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

class WebcoScraperWithSelenium:
    """Webco scraper that uses Selenium for JavaScript-heavy job pages"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'Webco',
            'website': 'https://webcotube.com',
            'jobboard_url': 'https://webcotube.wd12.myworkdayjobs.com/Webco/',
            'api_endpoint': 'https://webcotube.wd12.myworkdayjobs.com/wday/cxs/webcotube/Webco/jobs'
        }
        
        # Set up session headers for API calls
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })
    
    def clean_job_title(self, raw_title: str) -> str:
        """Remove location suffixes from job titles"""
        if not raw_title:
            return raw_title
        
        # Define locations to remove (based on your search filters)
        locations_to_remove = [
            'sand springs', 'tulsa', 'broken arrow', 'oklahoma', 'ok',
            'remote', 'hybrid', 'kellyville', 'mannford'
        ]
        
        cleaned_title = raw_title.strip()
        
        # Remove location suffixes (e.g., "Entry Level Manufacturing - Sand Springs")
        for location in locations_to_remove:
            # Remove " - Location" pattern
            pattern = r'\s*-\s*' + re.escape(location) + r'\s*$'
            cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE)
            
            # Remove ", Location" pattern  
            pattern = r'\s*,\s*' + re.escape(location) + r'\s*$'
            cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE)
            
            # Remove "(Location)" pattern
            pattern = r'\s*\(\s*' + re.escape(location) + r'\s*\)\s*$'
            cleaned_title = re.sub(pattern, '', cleaned_title, flags=re.IGNORECASE)
        
        return cleaned_title.strip()
    
    def establish_session(self) -> bool:
        """Establish session with Workday site"""
        try:
            logger.info("Establishing session with Webco careers page...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from Webco Workday API with location filters"""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None
        
        # Define the location filters from the URL
        location_facets = {
            "locations": [
                "d0fa73fbb5531007e6bc2740c4f50000",
                "d0fa73fbb5531007e785feed3b5f0000",
                "d0fa73fbb5531007e6bc59d7e24e0000",
                "d0fa73fbb5531007e6bc55a372ce0000"
            ]
        }
        
        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                
                body = {
                    "appliedFacets": location_facets,
                    "limit": limit,
                    "offset": offset,
                    "searchText": ""
                }
                
                response = self.session.post(
                    self.company_config['api_endpoint'],
                    json=body,
                    headers={
                        'Referer': self.company_config['jobboard_url'],
                        'Origin': 'https://webcotube.wd12.myworkdayjobs.com',
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
 
    def extract_job_content(self, html_content: str) -> str:
        """Extract job content from HTML with better consistency"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Remove scripts, styles, navigation completely first
            for tag in soup.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside']):
                tag.decompose()
            
            # Try multiple Workday-specific selectors in order of preference
            job_selectors = [
                # Most specific Workday selectors
                '[data-automation-id="jobPostingDescription"]',
                '[data-automation-id="jobDescription"]', 
                '[data-automation-id="richTextEditor"]',
                '.jobPostingDescription',
                '.wd-rich-text-area',
                
                # General job content selectors
                '.job-description',
                '.job-details',
                '[role="main"] .css-1',
                'main .css-1',
                
                # Last resort - any substantial content div
                'div[class*="css-"]:has(p)',
                'div:has(h1, h2, h3)'
            ]
            
            extracted_content = None
            selector_used = None
            
            for selector in job_selectors:
                try:
                    content = soup.select_one(selector)
                    if content:
                        # Check if this content has substantial text
                        text_content = content.get_text(strip=True)
                        if len(text_content) > 200:  # Minimum meaningful content
                            # Clean up the content before returning
                            extracted_content = self._clean_extracted_content(content)
                            selector_used = selector
                            break
                except Exception as e:
                    logger.debug(f"  Error with selector {selector}: {e}")
                    continue
            
            if extracted_content:
                logger.info(f"  Extracted content using selector: {selector_used} ({len(extracted_content)} chars)")
                return extracted_content
            
            # Fallback: try to find the main content area manually
            logger.warning("  No specific selectors worked, trying fallback method...")
            return self._fallback_content_extraction(soup)
            
        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return self._minimal_cleanup(html_content)

    def _clean_extracted_content(self, content_element) -> str:
        """Clean extracted content to remove unwanted elements"""
        # Remove any remaining style, script, or navigation elements
        for tag in content_element.find_all(['style', 'script', 'nav', 'header', 'footer']):
            tag.decompose()
        
        # Remove elements with navigation-like classes
        nav_classes = ['nav', 'menu', 'header', 'footer', 'breadcrumb', 'pagination']
        for nav_class in nav_classes:
            for element in content_element.find_all(class_=lambda x: x and nav_class in x.lower()):
                element.decompose()
        
        # Convert to string and clean up
        content_str = str(content_element)
        
        # Remove excessive whitespace
        content_str = re.sub(r'\n\s*\n\s*\n', '\n\n', content_str)
        
        return content_str

    def _fallback_content_extraction(self, soup) -> str:
        """Fallback method to extract meaningful content"""
        # Look for divs that contain job-like content
        potential_containers = soup.find_all('div')
        
        best_content = ""
        best_score = 0
        
        for div in potential_containers:
            text = div.get_text(strip=True)
            
            # Score based on job posting keywords and length
            score = 0
            if len(text) > 300:
                score += 1
            if any(keyword in text.lower() for keyword in ['responsibilities', 'qualifications', 'requirements', 'experience', 'skills']):
                score += 2
            if any(keyword in text.lower() for keyword in ['bachelor', 'degree', 'years', 'position', 'role']):
                score += 1
            
            if score > best_score:
                best_score = score
                best_content = str(div)
        
        if best_content:
            logger.info(f"  Used fallback extraction (score: {best_score})")
            return self._clean_extracted_content(BeautifulSoup(best_content, 'html.parser'))
        
        # Last resort - return body with minimal cleanup
        body = soup.find('body')
        if body:
            return self._minimal_cleanup(str(body))
        
        return self._minimal_cleanup(str(soup))

    def _minimal_cleanup(self, html_content: str) -> str:
        """Minimal cleanup when other methods fail"""
        # Remove obvious unwanted content
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        
        # If it starts with full HTML document, try to extract just the body
        if html_content.strip().startswith('<html'):
            soup = BeautifulSoup(html_content, 'html.parser')
            body = soup.find('body')
            if body:
                return str(body)
        
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
            
            # Step 3: Get job listings with location filters
            logger.info("Step 3: Getting job listings from API with location filters...")
            all_jobs = self.get_job_listings()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            logger.info(f"✓ Retrieved {len(all_jobs)} jobs from API")
            
            stats['found'] = len(all_jobs)
            
            # Step 4: Process each job with Selenium
            for i, job in enumerate(all_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(all_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Clean the job title to remove location suffixes
                    original_title = job.get('title', '')
                    cleaned_title = self.clean_job_title(original_title)
                    
                    if original_title != cleaned_title:
                        logger.info(f"  Cleaned title: '{original_title}' → '{cleaned_title}'")
                    
                    # Build job URL
                    external_path = job.get('externalPath', '')
                    if not external_path:
                        logger.warning(f"  No externalPath found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://webcotube.wd12.myworkdayjobs.com/Webco{external_path}"
                    logger.info(f"  Job URL: {job_url}")
                    
                    # Download job details with Selenium
                    job_html = self.download_job_details(job_url)
                    if not job_html or len(job_html.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    logger.info(f"  Downloaded job content: {len(job_html)} chars")
                    
                    # Prepare job data for database
                    job_data = {
                        'title': cleaned_title,
                        'url': job_url,
                        'description': job_html,
                        'date_posted': parse_relative_date(job.get('postedOn', '')),
                        'scraping_hash': self.create_scraping_hash({
                            'title': cleaned_title,
                            'url': job_url,
                            'description': job_html
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(0.5)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 5: Mark scrape as completed and close old jobs
            logger.info("Step 5: Marking scrape as completed...")
            self.db.mark_scrape_completed(company_id)
            
            logger.info("Step 6: Marking old jobs as closed...")
            self.db.mark_old_jobs_closed(company_id, scrape_start_time)
            
            # Step 7: Log results
            logger.info("Step 7: Logging results...")
            self.db.log_scraping_activity('Webco Workday', stats)
            
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
        scraper = WebcoScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting Webco job scraping with Selenium...")
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
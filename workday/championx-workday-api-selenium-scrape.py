#!/usr/bin/env python3
"""
championx-workday-api-selenium-scrape.py
ChampionX Workday Job Scraper
Uses ChampionX's Workday API with pre-filtered Tulsa/Broken Arrow locations
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
        logging.FileHandler('championx_scraper.log', encoding='utf-8'),
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
    
    def get_company_id(self) -> int:
        """Get ChampionX company ID (already exists in database)"""
        return 623
    
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
                        Function = %s,
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
                        Function, Approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'ChampionX Workday',
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
        """Map job title to function ID using keywords"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords
        function_keywords = {
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data',
                'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'cyber'
            ],
            'Engineering': [
                'mechanical', 'mech eng', 'mechanical engineer', 'electrical', 'elec eng',
                'electrical engineer', 'civil', 'civil engineer',
            ],
            'Accounting': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
            'Operations': [
                'operations', 'ops', 'supply chain', 'logistics', 'process', 'facility',
                'project manager', 'program manager', 'scrum master', 'project coordinator',
            ],
            'Customer Support': ['customer service', 'support', 'help desk', 'call center', 'client'],
            'Administrative': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
            'Manufacturing': ['manufacturing', 'production', 'plant', 'facility', 'assembly'],
            'Quality': ['quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'],
            'Security': ['security', 'safety', 'guard', 'protection'],
            'Skilled Trades': ['operator', 'technician', 'maintenance', 'mechanic', 'welder', 'electrician'],
            'Healthcare': ['nurse', 'doctor', 'medical', 'healthcare', 'clinical', 'physician']
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
            
            # Performance optimizations
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            # Additional GPU error suppression
            chrome_options.add_argument('--disable-gpu-sandbox')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--disable-background-timer-throttling')
            chrome_options.add_argument('--disable-backgrounding-occluded-windows')
            chrome_options.add_argument('--disable-renderer-backgrounding')
            chrome_options.add_argument('--disable-features=VizDisplayCompositor')
            chrome_options.add_argument('--use-gl=swiftshader')
            chrome_options.add_argument('--disable-ipc-flooding-protection')
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
            chrome_options.add_argument('--disable-gpu-logging')
            chrome_options.add_argument('--disable-logging')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            
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

class ChampionXScraperWithSelenium:
    """ChampionX scraper that uses Workday API + Selenium for job details"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'ChampionX',
            'website': 'https://www.championx.com/',
            'jobboard_url': 'https://championx.wd1.myworkdayjobs.com/ChampionX_External?locations=292b680d8051014bb0149902a674a16b&locations=292b680d805101cb9d2a8402a674886b',
            'api_endpoint': 'https://championx.wd1.myworkdayjobs.com/wday/cxs/championx/ChampionX_External/jobs'
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
            logger.info("Establishing session with ChampionX careers page...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            logger.info("Session established successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to establish session: {e}")
            return False
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from ChampionX Workday API"""
        all_jobs = []
        limit = 20
        offset = 0
        total_results = None
        
        while True:
            try:
                logger.info(f"Fetching jobs with offset: {offset}")
                
                body = {
                    "appliedFacets": {
                        "locations": [
                            "292b680d8051014bb0149902a674a16b",
                            "292b680d805101cb9d2a8402a674886b"
                        ]
                    },
                    "limit": limit,
                    "offset": offset,
                    "searchText": ""
                }
                
                response = self.session.post(
                    self.company_config['api_endpoint'],
                    json=body,
                    headers={
                        'Referer': self.company_config['jobboard_url'],
                        'Origin': 'https://championx.wd1.myworkdayjobs.com',
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
        """Main scraping method"""
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
            logger.info("Step 2: Getting company ID...")
            company_id = self.db.get_company_id()
            logger.info(f"? Company ID: {company_id}")
            
            # Step 3: Get job listings
            logger.info("Step 3: Getting job listings from API...")
            all_jobs = self.get_job_listings()
            if not all_jobs:
                raise Exception("No jobs retrieved from API")
            logger.info(f"? Retrieved {len(all_jobs)} jobs from API")
            
            stats['found'] = len(all_jobs)
            
            # Step 4: Process each job with Selenium
            for i, job in enumerate(all_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(all_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Build job URL
                    external_path = job.get('externalPath', '')
                    if not external_path:
                        logger.warning(f"  No externalPath found")
                        stats['skipped'] += 1
                        continue
                    
                    job_url = f"https://championx.wd1.myworkdayjobs.com/ChampionX_External{external_path}"
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
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ? Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(0.5)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 5: Mark old jobs as closed
            logger.info("Step 5: Marking old jobs as closed...")
            cutoff_date = datetime.now() - timedelta(days=7)
            self.db.mark_old_jobs_closed(company_id, cutoff_date)
            
            # Step 6: Log results
            logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('ChampionX Workday', stats)
            
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
    
    db_host = os.getenv('POSTGRES_HOST', 'localhost')
    db_connection = f"postgresql://postgres:{db_password}@{db_host}:5432/tulsa_jobs"
    
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = ChampionXScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting ChampionX job scraping...")
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
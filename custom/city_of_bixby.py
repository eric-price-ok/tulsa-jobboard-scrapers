#!/usr/bin/env python3
"""
bixby-jobs-scraper.py
City of Bixby Job Board Scraper
Handles City of Bixby job board with Selenium for full job description extraction
"""

from utils.selenium_config import SeleniumConfig
from utils.posting_operations import store_job_listing, load_active_jobs_cache, check_job_in_cache, update_job_verified_timestamp, mark_stale_jobs_closed
from utils.utility_methods import setup_logging
from utils.company_operations import get_company_config_by_name
from utils.db_connection import get_database_connection
from utils.date_utilities import normalize_date_string
from utils.location_utilities import get_city_id
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import time
import hashlib
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str = None):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}
        self.logger = None
    
    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)
    
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache"""
        return check_job_in_cache(job_url, self.active_jobs_cache)

    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)
    
    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing using posting_operations"""
        with self.conn.cursor() as cursor:
            city_id = get_city_id(cursor, 'Bixby')
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'company_id': company_id,
                'job_type_id': self._map_job_type(job_data.get('position_type', '')),
                'function': self._map_job_function(job_data.get('job_title', '')),
                'office_location_id': 1,  # Default to In Office for government jobs
                'city_id': city_id
            })
            return store_job_listing(cursor, enhanced_job_data, company_id, 'City of Bixby')
    
    def _map_job_type(self, position_type: str) -> Optional[int]:
        """Map position type to job_type_id using LIKE matching"""
        if not position_type:
            return None
            
        position_type_lower = position_type.lower()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM jobtype WHERE LOWER(name) LIKE %s", (f"%{position_type_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{position_type}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            position_mappings = {
                'full time': ['full time', 'full-time', 'fulltime', 'permanent', 'regular'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor', 'contractual']
            }
            
            for job_type_key, variations in position_mappings.items():
                if any(var in position_type_lower for var in variations):
                    cursor.execute("SELECT id FROM jobtype WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{position_type}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        self.logger.warning(f"  Could not map '{position_type}' to any job type")
        return None
    
    def _map_job_function(self, job_title: str) -> Optional[int]:
        """Map job title to function with government-specific mappings, default to 'Other'"""
        job_title_lower = (job_title or '').lower()

        with self.conn.cursor() as cursor:
            function_keywords = {
                'Information Technology': ['technology', 'it', 'software', 'tech', 'data', 'systems', 'computer', 'network'],
                'Engineering': ['engineering', 'engineer', 'civil', 'mechanical', 'electrical'],
                'Finance': ['finance', 'financial', 'accounting', 'accountant', 'budget', 'treasurer'],
                'Human Resources': ['hr', 'human resources', 'people', 'personnel'],
                'Administration': ['admin', 'administrative', 'clerk', 'coordinator', 'assistant', 'secretary'],
                'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'paralegal'],
                'Public Safety': ['police', 'fire', 'safety', 'security', 'emergency', 'dispatcher'],
                'Operations': ['operations', 'maintenance', 'facility', 'utilities', 'public works'],
                'Planning': ['planning', 'planner', 'zoning', 'development'],
                'Parks and Recreation': ['parks', 'recreation', 'athletic', 'grounds', 'maintenance'],
                'Customer Service': ['customer', 'service', 'support', 'reception', 'front desk'],
                'Transportation/Logistics': ['transportation', 'logistics', 'fleet', 'driver'],
                'Healthcare Provider': ['nurse', 'medical', 'healthcare', 'clinical'],
                'Skilled Labor': ['operator', 'technician', 'maintenance', 'mechanic', 'welder', 'electrician']
            }

            if job_title_lower:
                for function_name, keywords in function_keywords.items():
                    if any(keyword in job_title_lower for keyword in keywords):
                        cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            self.logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                            return result['id']

            cursor.execute("SELECT id FROM functions WHERE name = 'Other'")
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{job_title}' to default function: Other")
                return result['id']

        self.logger.warning(f"  'Other' function not found in database")
        return None
    
    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE company
                SET last_full_scrape_completed = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")
    
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
    
    def __init__(self, headless=True, logger=None):
        self.driver = None
        self.headless = headless
        self.logger = logger or logging.getLogger(__name__)
        self.setup_driver()
    
    def setup_driver(self):
        """Initialize Chrome WebDriver with optimized options"""
        try:
            chrome_options = SeleniumConfig.get_chrome_options(self.headless)

            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)

            SeleniumConfig.setup_driver_timeouts(self.driver)

            self.logger.info("Optimized Selenium WebDriver initialized")

        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            self.logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            # Wait for basic page structure
            wait = WebDriverWait(self.driver, timeout)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                self.logger.warning(f"  Body tag not found within timeout")
                return ""
            
            # Give minimal time for dynamic content
            time.sleep(1.5)
            
            # Get page source
            page_source = self.driver.page_source
            self.logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source
                
        except TimeoutException:
            self.logger.warning(f"  Timeout waiting for page to load")
            return self.driver.page_source if self.driver else ""
            
        except Exception as e:
            self.logger.error(f"  Error loading job page: {e}")
            return ""
    
    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Load job board and extract all job listings"""
        try:
            self.logger.info(f"Loading job board: {job_board_url}")
            self.driver.get(job_board_url)
            
            # Wait for job listings to load
            wait = WebDriverWait(self.driver, 20)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'div.job')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job divs
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, 'div.job')
            self.logger.info(f"Found {len(job_elements)} job opportunities")
            
            jobs = []
            for i, job_element in enumerate(job_elements):
                try:
                    job_data = self.extract_job_metadata(job_element, i + 1)
                    if job_data:
                        jobs.append(job_data)
                except Exception as e:
                    self.logger.error(f"Error extracting job {i + 1}: {e}")
            
            self.logger.info(f"Successfully extracted {len(jobs)} job listings")
            return jobs
            
        except TimeoutException:
            self.logger.error("Timeout waiting for job listings to load")
            return []
        except Exception as e:
            self.logger.error(f"Error loading job board: {e}")
            return []
    
    def extract_job_metadata(self, job_element, job_number: int) -> Dict:
        """Extract job metadata from a single job element"""
        job_data = {}
        
        try:
            # Job Title and URL
            title_link = job_element.find_element(By.CSS_SELECTOR, 'a[id^="jobTitle_"]')
            job_data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href')
            
            # Build full URL
            if href.startswith('http'):
                job_data['posting_url'] = href
            elif href.startswith('/'):
                job_data['posting_url'] = f"https://www.bixbyok.gov{href}"
            else:
                job_data['posting_url'] = f"https://www.bixbyok.gov/{href}"
            
            # Extract JobID from href for posting_id
            job_id_match = re.search(r'JobID=([^&]+)', href)
            job_data['posting_id'] = job_id_match.group(1) if job_id_match else None
            
            # Posted Date (from hidden span)
            try:
                posted_date_element = job_element.find_element(By.CSS_SELECTOR, 'span[id^="jobStartDate_"]')
                job_data['posted_date_raw'] = posted_date_element.text.strip()
                job_data['date_posted'] = normalize_date_string(job_data['posted_date_raw'])
            except NoSuchElementException:
                job_data['posted_date_raw'] = None
                job_data['date_posted'] = None
            
            self.logger.info(f"Job {job_number}: {job_data['job_title']} - {job_data['posted_date_raw']}")
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None
    
    def extract_job_description_and_details(self, html_content: str) -> Dict:
        """Extract job description and position type from HTML content"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            position_type = None

            def extract_position_type(text):
                m = re.search(r'Position Type[:\s]+([^\n\r]+)', text, re.IGNORECASE)
                if m:
                    return re.sub(r'\s*(Department|Location|Salary).*$', '', m.group(1).strip(), flags=re.IGNORECASE)
                return None

            # Try targeted containers first
            for selector in [
                'div.job-description',
                'section.job-description',
                'div#job-description',
                'div.description',
                'article',
                'main',
                'div[role="main"]',
            ]:
                container = soup.select_one(selector)
                if container:
                    text = re.sub(r'\s+', ' ', container.get_text(separator=' ', strip=True)).strip()
                    if len(text) > 100:
                        position_type = extract_position_type(text)
                        self.logger.info(f"  Extracted job description via '{selector}': {len(text)} characters")
                        return {'job_description': text[:50000], 'position_type': position_type}

            # Fall back to full body with aggressive noise stripping
            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer',
                                          'aside', 'button', 'form', 'input', 'select', 'label',
                                          'iframe', 'figure', 'picture']):
                    tag.decompose()

                for cls in ['breadcrumb', 'cookie', 'modal', 'overlay', 'pagination',
                            'menu', 'toolbar', 'banner', 'sidebar', 'alert']:
                    for tag in body.find_all(class_=lambda c: c and cls in ' '.join(c).lower()):
                        tag.decompose()

                body_text = re.sub(r'\s+', ' ', body.get_text(separator=' ', strip=True)).strip()
                if len(body_text) > 100:
                    position_type = extract_position_type(body_text)
                    self.logger.info(f"  Extracted job description from body: {len(body_text)} characters")
                    return {'job_description': body_text[:50000], 'position_type': position_type}

            self.logger.warning(f"  No meaningful job description found")
            return {'job_description': html_content[:50000], 'position_type': None}

        except Exception as e:
            self.logger.warning(f"Error extracting job details: {e}")
            return {'job_description': html_content[:50000], 'position_type': None}
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class BixbyJobScraper:
    """City of Bixby job scraper"""
    COMPANY_NAME = 'City of Bixby'
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        # Retrieve the website, jobboard, company_id, common_name from company_operations.py
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        # Store company ID for later use
        self.company_id = self.company_config['id']        
        # Configure logging using company name
        self.logger = setup_logging(self.company_config['name'])
        self.db.logger = self.logger

        self.selenium_scraper = SeleniumJobScraper(headless=True, logger=self.logger)

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
            # Step 1: Company already retrieved during initialization
            self.logger.info("Step 1: Using company from database")

            # Step 1.5: Load active jobs cache
            self.logger.info("Step 1.5: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            # Step 2: Get job listings from job board
            self.logger.info("Step 2: Getting job listings from Bixby job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            self.logger.info(f"? Found {len(job_listings)} jobs")
            
            # Step 3: Process each job
            for i, job_metadata in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")
                    
                    # Check if job already exists using cache
                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue
                    
                    # Scrape full job description for new jobs only
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        self.logger.warning(f"  Failed to get meaningful job content, storing with limited info")
                        # Store with limited information
                        job_details = {
                            'job_description': f"Job Title: {job_metadata['job_title']}\nPosted: {job_metadata.get('posted_date_raw', 'Unknown')}",
                            'position_type': None
                        }
                    else:
                        job_details = self.selenium_scraper.extract_job_description_and_details(job_html)
                    
                    # Prepare complete job data
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': job_metadata['posting_id'],
                        'job_description': job_details['job_description'],
                        'date_posted': job_metadata['date_posted'],
                        'position_type': job_details['position_type'],
                        'scraping_hash': self.create_scraping_hash({
                            'job_title': job_metadata['job_title'],
                            'posting_url': job_metadata['posting_url'],
                            'job_description': job_details['job_description']
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ? Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1.0)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_metadata.get('job_title', 'Unknown')}: {e}"
                    self.logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark stale jobs as closed
            self.logger.info("Step 4: Marking stale jobs as closed...")
            with self.db.conn.cursor() as cursor:
                mark_stale_jobs_closed(cursor, self.company_id, self.logger)
            
            # Step 5: Update company scrape completion
            self.logger.info("Step 5: Updating company scrape completion...")
            self.db.update_company_scrape_completed(self.company_id)
            
            # Step 6: Log results
            self.logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('City of Bixby', stats)
            
        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            self.logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats
    
    def cleanup(self):
        """Clean up resources"""
        if self.selenium_scraper:
            self.selenium_scraper.cleanup()

def main():
    """Main execution function"""
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager("")
        scraper = BixbyJobScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting City of Bixby job scraping...")
        results = scraper.scrape_jobs()
        
        # Print summary
        scraper.logger.info("=== SCRAPING SUMMARY ===")
        scraper.logger.info(f"Jobs found: {results['found']}")
        scraper.logger.info(f"Jobs added: {results['added']}")
        scraper.logger.info(f"Jobs updated: {results['updated']}")
        scraper.logger.info(f"Jobs skipped: {results['skipped']}")
        scraper.logger.info(f"Errors: {len(results['errors'])}")
        
        if results['errors']:
            scraper.logger.error("Errors encountered:")
            for error in results['errors']:
                scraper.logger.error(f"  - {error}")
        
    except Exception as e:
        if scraper and hasattr(scraper, 'logger'):
            scraper.logger.error(f"Script failed: {e}")
        else:
            print(f"Script failed: {e}")  # Fallback to print if logger not available
        return 1

    finally:
        if scraper:
            scraper.cleanup()
    
    return 0

if __name__ == "__main__":
    exit(main())
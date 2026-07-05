#!/usr/bin/env python3
"""
broken-arrow-governmentjobs-scrape.py
City of Broken Arrow Government Jobs Scraper
Handles governmentjobs.com job boards with Selenium for full job description extraction
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
from urllib.parse import urljoin

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str = None):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}  # This caches current jobs from db to compare against
        self.logger = None

    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company where status_id != 6"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)
        
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache first, then database"""
        # First check cache
        job_id = check_job_in_cache(job_url, self.active_jobs_cache)
        if job_id:
            return job_id
        # If not in cache, this is a new job
        return None
    
    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing using posting_operations"""
        with self.conn.cursor() as cursor:
            city_id = get_city_id(cursor, 'Broken Arrow')
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'company_id': company_id,
                'job_type_id': self._map_job_type(job_data.get('job_type', '')),
                'function': self._map_job_function(job_data.get('job_title', '')),
                'office_location_id': 1,  # Default to In Office for government jobs
                'city_id': city_id
            })

            return store_job_listing(cursor, enhanced_job_data, company_id, 'Broken Arrow Government Jobs')

    def _map_job_type(self, job_type_str: str) -> Optional[int]:
        """Map job type string to JobType table, strip hourly/annually first"""
        if not job_type_str:
            return None
            
        # Strip common suffixes
        job_type_clean = job_type_str.lower()
        for suffix in ['hourly', 'annually', 'per hour', 'per year', 'annual', 'hr']:
            job_type_clean = job_type_clean.replace(suffix, '').strip()
        
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM jobtype WHERE LOWER(name) LIKE %s", (f"%{job_type_clean}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{job_type_str}' to job type ID: {result['id']}")
                return result['id']
            
            # Try common variations
            job_type_mappings = {
                'full time': ['full time', 'full-time', 'fulltime', 'permanent', 'regular'],
                'part time': ['part time', 'part-time', 'parttime'],
                'contract': ['contract', 'contractor', 'temporary', 'temp'],
                'internship': ['intern', 'internship'],
                'seasonal': ['seasonal']
            }
            
            for job_type_key, variations in job_type_mappings.items():
                if any(var in job_type_clean for var in variations):
                    cursor.execute("SELECT id FROM jobtype WHERE LOWER(name) LIKE %s", (f"%{job_type_key}%",))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_type_str}' to job type ID: {result['id']} via '{job_type_key}'")
                        return result['id']
        
        self.logger.warning(f"  Could not map '{job_type_str}' to any job type")
        return None
    
    def _map_job_function(self, job_title: str) -> Optional[int]:
        """Map job title to function with government-specific mappings, default to 'Other'"""
        job_title_lower = (job_title or '').lower()

        with self.conn.cursor() as cursor:
            function_keywords = {
                'Law Enforcement': ['police', 'fire', 'emergency', '911', 'dispatcher', 'security', 'officer', 'detective', 'firefighter', 'ems', 'paramedic'],
                'Operations': [
                    'utilities', 'water', 'sewer', 'streets', 'maintenance', 'facilities', 'public works', 'infrastructure', 'roads',
                    'parks', 'recreation', 'sports', 'community center', 'recreation center', 'lifeguard', 'coach',
                    'fleet', 'vehicle', 'equipment', 'driver', 'mechanic', 'transportation',
                ],
                'Administrative': ['clerk', 'admin', 'administrative', 'finance', 'hr', 'human resources', 'budget', 'treasurer', 'accounting', 'receptionist', 'assistant'],
                'Legal': ['legal', 'attorney', 'prosecutor', 'court', 'judge', 'bailiff', 'legal assistant'],
                'Engineering': ['engineer', 'engineering', 'planner', 'planning', 'zoning', 'building', 'inspection', 'development', 'architect'],
                'Information Technology': ['it', 'information technology', 'technology', 'systems', 'data', 'network', 'computer', 'software', 'tech'],
                'Customer Support': ['customer', 'service', 'support', 'public', 'citizen']
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
        
            # Try to find chromedriver
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
        
            # Apply standard timeouts and settings
            SeleniumConfig.setup_driver_timeouts(self.driver)
        
            self.logger.info("Optimized Selenium WebDriver initialized")
        
        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render - optimized for speed"""
        try:
            self.logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            # Shorter, more targeted waits
            wait = WebDriverWait(self.driver, timeout)
            
            # Wait for basic page structure
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
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'ul.search-results-listing-container.job-listing-container')))
            
            # Give additional time for all content to load
            time.sleep(3)
            
            # Find all job opportunity items
            job_elements = self.driver.find_elements(By.CSS_SELECTOR, 'ul.search-results-listing-container.job-listing-container li.list-item[data-job-id]')
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
            title_link = job_element.find_element(By.CSS_SELECTOR, 'a.item-details-link')
            job_data['job_title'] = title_link.text.strip()
            href = title_link.get_attribute('href')
            self.logger.info(f"  Raw href value: {href}")
            
            # Handle both absolute and relative URLs
            base_url = "https://www.governmentjobs.com"
            if href.startswith('http'):
                job_data['posting_url'] = href
            else:
                # urljoin handles both '/path' and 'path' correctly
                job_data['posting_url'] = urljoin(base_url, href)

            # Get job ID from data attribute
            job_data['posting_id'] = job_element.get_attribute('data-job-id')
            
            self.logger.info(f"Job {job_number}: {job_data['job_title']} - ID: {job_data['posting_id']}")
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None
    
    def extract_job_description(self, html_content: str) -> str:
        """Extract job description from HTML content"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            # Try targeted containers first (governmentjobs.com selectors)
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
                        self.logger.info(f"  Extracted job description via '{selector}': {len(text)} characters")
                        return text[:50000]

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
                    self.logger.info(f"  Extracted job description from body: {len(body_text)} characters")
                    return body_text[:50000]

            self.logger.warning(f"  No meaningful job description found")
            return html_content[:50000]

        except Exception as e:
            self.logger.warning(f"Error extracting job description: {e}")
            return html_content[:50000]
    
    def parse_salary(self, salary_text: str) -> tuple:
        """Parse salary string and return (min_salary, max_salary, frequency)"""
        if not salary_text:
            return None, None, None
            
        # Remove common prefixes and clean up
        salary_clean = salary_text.replace('$', '').replace(',', '').strip()
        
        # Determine frequency
        frequency = None
        if any(term in salary_text.lower() for term in ['hourly', 'per hour', '/hour', 'hr']):
            frequency = 'Hourly'
        elif any(term in salary_text.lower() for term in ['annually', 'per year', '/year', 'annual']):
            frequency = 'Annually'
        
        # Look for range pattern (e.g., "50000 - 60000")
        range_match = re.search(r'(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)', salary_clean)
        if range_match:
            min_salary = float(range_match.group(1))
            max_salary = float(range_match.group(2))
            return min_salary, max_salary, frequency
        
        # Look for single value
        single_match = re.search(r'(\d+(?:\.\d+)?)', salary_clean)
        if single_match:
            salary_value = float(single_match.group(1))
            return salary_value, salary_value, frequency
        
        return None, None, frequency
    
    def extract_job_details(self, html_content: str) -> Dict:
        """Extract detailed job information from job page HTML"""
        details = {}
        
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Extract salary information
            salary_label = soup.find('div', {'id': 'salary-label-id'})
            if salary_label:
                # Look for salary text immediately after the salary label
                salary_text = ""
                next_element = salary_label.next_sibling
                while next_element:
                    if hasattr(next_element, 'get_text'):
                        text = next_element.get_text(strip=True)
                        if text and any(char.isdigit() for char in text):
                            salary_text = text
                            break
                    elif isinstance(next_element, str) and next_element.strip():
                        if any(char.isdigit() for char in next_element):
                            salary_text = next_element.strip()
                            break
                    next_element = next_element.next_sibling
                
                if salary_text:
                    min_sal, max_sal, freq = self.parse_salary(salary_text)
                    details['minimum_salary'] = min_sal
                    details['maximum_salary'] = max_sal
                    details['pay_frequency'] = freq
            
            # Extract term descriptions (Job Type, Opening Date, Closing Date)
            term_descriptions = soup.find_all('div', {'id': 'term-description'})
            
            for term_div in term_descriptions:
                # Look for the name/label of this term
                term_name = None
                
                # Check for a label or strong tag before this div
                prev_element = term_div.previous_sibling
                while prev_element:
                    if hasattr(prev_element, 'get_text'):
                        text = prev_element.get_text(strip=True)
                        if text:
                            term_name = text
                            break
                    elif isinstance(prev_element, str) and prev_element.strip():
                        term_name = prev_element.strip()
                        break
                    prev_element = prev_element.previous_sibling
                
                # Also check within the div for a label
                if not term_name:
                    label = term_div.find(['strong', 'label', 'dt'])
                    if label:
                        term_name = label.get_text(strip=True)
                
                if term_name:
                    term_value = term_div.get_text(strip=True)
                    
                    if 'job type' in term_name.lower():
                        details['job_type'] = term_value
                    elif 'opening date' in term_name.lower():
                        details['date_posted'] = normalize_date_string(term_value)
                    elif 'closing date' in term_name.lower():
                        details['date_closed'] = normalize_date_string(term_value)
            
            return details
            
        except Exception as e:
            self.logger.error(f"Error extracting job details: {e}")
            return {}
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class BrokenArrowGovJobsScraper:
    """City of Broken Arrow Government Jobs scraper"""
    COMPANY_NAME = 'City of Broken Arrow'
    
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
            
            # Step 1.5: Load active jobs cache (status_id != 6)
            self.logger.info("Step 1.5: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            # Step 2: Get job listings from job board
            self.logger.info("Step 2: Getting job listings from Broken Arrow job board...")
            job_listings = self.selenium_scraper.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            self.logger.info(f"✓ Found {len(job_listings)} jobs")
            
            # Step 3: Process each job
            for i, job_metadata in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_metadata.get('job_title', 'Unknown')}")
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_metadata['posting_url'])
                    if existing_job_id:
                        # Update timestamp since we found the job on current scrape
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue

                    # Scrape full job description for new jobs only
                    job_html = self.selenium_scraper.get_job_content(job_metadata['posting_url'])
                    if not job_html or len(job_html.strip()) < 100:
                        self.logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    # Extract job description and additional details
                    job_description = self.selenium_scraper.extract_job_description(job_html)
                    job_details = self.selenium_scraper.extract_job_details(job_html)
                    
                    # Prepare complete job data
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': job_metadata['posting_id'],
                        'job_description': job_description,
                        'date_posted': job_details.get('date_posted'),
                        'date_closed': job_details.get('date_closed'),
                        'job_type': job_details.get('job_type'),
                        'minimum_salary': job_details.get('minimum_salary'),
                        'maximum_salary': job_details.get('maximum_salary'),
                        'pay_frequency': job_details.get('pay_frequency'),
                        'scraping_hash': None
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ✓ Stored job with ID: {job_id}")
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
            self.db.log_scraping_activity('Broken Arrow Government Jobs', stats)
            
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
        scraper = BrokenArrowGovJobsScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting Broken Arrow Government Jobs scraping...")
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
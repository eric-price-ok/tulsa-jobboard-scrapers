#!/usr/bin/env python3
"""
sandsprings-scraper.py
City of Sand Springs Job Board Scraper
Scrapes http://sandspringsok.org/162/Job-Opportunities
"""

from utils.selenium_config import SeleniumConfig
from utils.posting_operations import store_job_listing, load_active_jobs_cache, check_job_in_cache, update_job_verified_timestamp, mark_stale_jobs_closed
from utils.utility_methods import setup_logging
from utils.company_operations import get_or_create_company, get_company_config_by_name, get_or_create_company_site
from utils.db_connection import get_database_connection
from utils.date_utilities import normalize_date_string
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import requests
import time
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

    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)
        
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache"""
        job_id = check_job_in_cache(job_url, self.active_jobs_cache)
        return job_id
    
    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing"""
        with self.conn.cursor() as cursor:
            # Map job type if provided
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'company_id': company_id,
                'job_type_id': self._map_job_type(job_data.get('position_type', '')),
                'function': self._map_government_job_function(job_data.get('job_title', ''))
            })
        
            return store_job_listing(cursor, enhanced_job_data, company_id, 'Sand Springs')

    def _map_job_type(self, position_type: str) -> Optional[int]:
        """Map position type to job_type_id"""
        if not position_type:
            return None
            
        position_type_lower = position_type.lower()
        
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM JobType WHERE LOWER(name) LIKE %s", (f"%{position_type_lower}%",))
            result = cursor.fetchone()
            if result:
                self.logger.info(f"  Mapped '{position_type}' to job type ID: {result['id']}")
                return result['id']
        
        return None
    
    def _map_government_job_function(self, job_title: str) -> int:
        """Map job title to government function, default to 'Other' (ID 32)"""
        if not job_title:
            return 32
            
        job_title_lower = job_title.lower()
        
        # Government-specific function mapping
        function_keywords = {
            'Public Safety': ['police', 'officer', 'detective', 'dispatcher', 'security', 'sheriff', 'deputy'],
            'Fire Department': ['fire', 'firefighter', 'ems', 'paramedic', 'emergency'],
            'Public Works': ['public works', 'maintenance', 'utilities', 'water', 'sewer', 'streets', 'parks'],
            'Administration': ['clerk', 'administrative', 'secretary', 'assistant', 'coordinator', 'manager', 'director'],
            'Finance': ['finance', 'accounting', 'budget', 'treasurer', 'auditor'],
            'Information Technology': ['it', 'technology', 'computer', 'network', 'systems'],
            'Legal': ['attorney', 'legal', 'prosecutor', 'counsel'],
            'Human Resources': ['hr', 'human resources', 'personnel'],
            'Engineering': ['engineer', 'engineering'],
            'Planning': ['planner', 'planning', 'zoning', 'development']
        }
        
        with self.conn.cursor() as cursor:
            for function_name, keywords in function_keywords.items():
                if any(keyword in job_title_lower for keyword in keywords):
                    cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                    result = cursor.fetchone()
                    if result:
                        self.logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                        return result['id']
        
        self.logger.info(f"  Mapped '{job_title}' to default function: Other (ID: 32)")
        return 32

    def get_or_create_company_site(self, company_id: int, location_name: str) -> int:
        """Get or create company site with Sand Springs city_id"""
        with self.conn.cursor() as cursor:
            return get_or_create_company_site(cursor, company_id, location_name, city_id=10, logger=self.logger)
    
    def update_company_scrape_completed(self, company_id: int):
        """Update last_full_scrape_completed timestamp for company"""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                UPDATE Company 
                SET last_full_scrape_completed = CURRENT_TIMESTAMP 
                WHERE id = %s
            """, (company_id,))
            self.logger.info(f"Updated last_full_scrape_completed for company {company_id}")
       
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
    """Handles individual job detail pages with Selenium"""
    
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
            self.logger.info("Selenium WebDriver initialized")
        
        except Exception as e:
            self.logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_details(self, job_url: str) -> Dict:
        """Load job detail page and extract all fields"""
        try:
            self.logger.info(f"  Loading job detail page...")
            self.driver.get(job_url)
            
            wait = WebDriverWait(self.driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(2)  # Allow dynamic content to load
            
            # Extract all fields
            job_details = {}
            
            # Date posted
            try:
                date_element = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ctl00_Posted")
                job_details['date_posted_raw'] = date_element.text.strip()
                job_details['date_posted'] = normalize_date_string(job_details['date_posted_raw'])
            except NoSuchElementException:
                job_details['date_posted'] = None
            
            # Salary range
            try:
                min_salary_element = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ctl00_WageMin")
                salary_text = min_salary_element.text.strip()
                job_details['minimum_salary'] = self._parse_salary(salary_text)
            except NoSuchElementException:
                job_details['minimum_salary'] = None
            
            try:
                max_salary_element = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ctl00_WageMax")
                salary_text = max_salary_element.text.strip()
                job_details['maximum_salary'] = self._parse_salary(salary_text)
            except NoSuchElementException:
                job_details['maximum_salary'] = None
            
            # Position type
            try:
                position_type_element = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ctl00_postitionTypeValue")
                job_details['position_type'] = position_type_element.text.strip()
            except NoSuchElementException:
                job_details['position_type'] = None
            
            # Location name
            try:
                location_element = self.driver.find_element(By.ID, "ctl00_ContentPlaceHolder1_ctl00_locationName")
                job_details['location_name'] = location_element.text.strip()
            except NoSuchElementException:
                job_details['location_name'] = None
            
            # Full job description (body content)
            try:
                body_element = self.driver.find_element(By.TAG_NAME, "body")
                job_details['job_description'] = body_element.get_attribute('innerHTML')
            except NoSuchElementException:
                job_details['job_description'] = ""
            
            # Determine pay frequency based on salary amount
            if job_details['minimum_salary'] or job_details['maximum_salary']:
                salary_value = job_details['minimum_salary'] or job_details['maximum_salary']
                job_details['pay_frequency'] = 'Annual' if salary_value >= 1000 else 'Hourly'
            else:
                job_details['pay_frequency'] = None
            
            return job_details
            
        except Exception as e:
            self.logger.error(f"  Error loading job details: {e}")
            return {}
    
    def _parse_salary(self, salary_text: str) -> Optional[float]:
        """Parse salary text to numeric value"""
        if not salary_text:
            return None
        
        # Remove commas and dollar signs, extract numbers
        clean_text = re.sub(r'[,$]', '', salary_text)
        match = re.search(r'[\d.]+', clean_text)
        
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass
        
        return None
    
    def cleanup(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                self.driver.quit()
                self.logger.info("WebDriver closed")
            except:
                pass

class SandSpringsJobScraper:
    """Sand Springs job scraper"""
    COMPANY_NAME = 'City of Sand Springs'
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        
        # Get company config
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        
        self.company_id = self.company_config['id']        
        self.logger = setup_logging(self.company_config['name'])
        self.db.logger = self.logger
        
        self.selenium_scraper = SeleniumJobScraper(headless=True, logger=self.logger)
        self.session = requests.Session()
        
        # Job board URL
        self.job_board_url = "http://sandspringsok.org/162/Job-Opportunities"
        
    def get_job_listings(self) -> List[Dict]:
        """Get job listings from the main job board page using Selenium"""
        try:
            self.logger.info(f"Loading job listings page with Selenium: {self.job_board_url}")
            self.selenium_scraper.driver.get(self.job_board_url)
        
            # Wait for basic page structure
            wait = WebDriverWait(self.selenium_scraper.driver, 20)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
            # Give extra time for dynamic content to load
            self.logger.info("Waiting for dynamic content to load...")
            time.sleep(5)  # Extended delay for content loading
        
            # Get page source and parse with BeautifulSoup
            page_source = self.selenium_scraper.driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')
        
            # Debug: Log some of the page content to see what we're getting
            self.logger.info(f"Page source length: {len(page_source)} characters")
        
            # Look for job listings in various possible patterns
            jobs = []
        
            # Original pattern: <div class="list-group-item"><h4 class="list-group-item-heading">
            list_items = soup.find_all('div', class_='list-group-item')
            self.logger.info(f"Found {len(list_items)} elements with class 'list-group-item'")
        
            for item in list_items:
                h4 = item.find('h4', class_='list-group-item-heading')
                if h4:
                    link = h4.find('a', target='_blank')
                    if link and link.get('href'):
                        job_data = {
                            'job_title': link.text.strip(),
                            'posting_url': link.get('href')
                        }
                        jobs.append(job_data)
                        self.logger.info(f"Found job: {job_data['job_title']}")
        
            # If no jobs found with original pattern, try alternative patterns
            if len(jobs) == 0:
                self.logger.info("No jobs found with original pattern, trying alternatives...")
            
                # Try to find any links to acquiretm.com
                all_links = soup.find_all('a', href=True)
                for link in all_links:
                    href = link.get('href', '')
                    if 'acquiretm.com' in href and 'job_details' in href:
                        job_data = {
                            'job_title': link.text.strip(),
                            'posting_url': href
                        }
                        if job_data['job_title']:  # Only add if we have a title
                            jobs.append(job_data)
                            self.logger.info(f"Found job via alternative pattern: {job_data['job_title']}")
            
                # Try looking for any job-related content
                if len(jobs) == 0:
                    # Log a portion of the page content for debugging
                    body_text = soup.get_text()
                    if 'job' in body_text.lower() or 'position' in body_text.lower():
                        self.logger.info("Page contains job-related text but no parseable job listings")
                        # Log first 500 characters of body text for debugging
                        self.logger.info(f"Page content sample: {body_text[:500]}")
                    else:
                        self.logger.info("Page does not appear to contain job-related content")
        
            self.logger.info(f"Total jobs found: {len(jobs)}")
            return jobs
        
        except Exception as e:
            self.logger.error(f"Error fetching job listings: {e}")
            return []
    
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
            # Step 1: Load active jobs cache
            self.logger.info("Step 1: Loading active jobs cache...")
            self.db.load_active_jobs_cache(self.company_id)

            # Step 2: Get job listings
            self.logger.info("Step 2: Getting job listings...")
            job_listings = self.get_job_listings()
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            
            # Step 3: Process each job
            for i, job_listing in enumerate(job_listings):
                try:
                    self.logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_listing['job_title']}")
                    
                    # Check if job already exists
                    existing_job_id = self.db.check_existing_job(job_listing['posting_url'])
                    if existing_job_id:
                        self.db.update_job_verified_timestamp(existing_job_id)
                        stats['updated'] += 1
                        continue
                    
                    # Get job details from detail page
                    job_details = self.selenium_scraper.get_job_details(job_listing['posting_url'])
                    
                    # Map location to company site
                    company_site_id = None
                    if job_details.get('location_name'):
                        company_site_id = self.db.get_or_create_company_site(
                            self.company_id, 
                            job_details['location_name']
                        )
                    
                    # Prepare job data
                    job_data = {
                        'job_title': job_listing['job_title'],
                        'posting_url': job_listing['posting_url'],
                        'job_description': job_details.get('job_description', ''),
                        'date_posted': job_details.get('date_posted'),
                        'minimum_salary': job_details.get('minimum_salary'),
                        'maximum_salary': job_details.get('maximum_salary'),
                        'pay_frequency': job_details.get('pay_frequency'),
                        'position_type': job_details.get('position_type'),
                        'company_site_id': company_site_id,
                        'city_id': 10,  # Sand Springs
                        'approved': True,
                        'job_status_id': 1
                    }
                    
                    # Store job
                    job_id = self.db.store_job_listing(job_data, self.company_id)
                    self.logger.info(f"  ? Stored job with ID: {job_id}")
                    stats['added'] += 1
                    
                    time.sleep(1.0)  # Be respectful
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_listing.get('job_title', 'Unknown')}: {e}"
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
            self.db.log_scraping_activity('Sand Springs', stats)
            
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
        db_manager = DatabaseManager()
        scraper = SandSpringsJobScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting Sand Springs job scraping...")
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
            print(f"Script failed: {e}")
        return 1

    finally:
        if scraper:
            scraper.cleanup()
    
    return 0

if __name__ == "__main__":
    exit(main())
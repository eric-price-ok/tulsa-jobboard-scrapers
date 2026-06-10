#!/usr/bin/env python3
"""
bancfirst-adp-api-selenium-scrap.py
BancFirst Corporation ADP Job Board Scraper
Clean hybrid approach: DOM extraction + API data enrichment
"""

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
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
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bancfirst_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.conn = None
        self.company_id = 566  # Hardcoded BancFirst company ID
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
        """Check if job URL already exists, update timestamp if found"""
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM JobListings WHERE posting_url = %s", (job_url,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("UPDATE JobListings SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (existing['id'],))
                logger.info(f"  Job already exists (ID: {existing['id']}), updated timestamp")
                return existing['id']
            return None
    
    def store_job_listing(self, job_data: Dict) -> int:
        """Store new job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            function = self._map_job_to_function(job_data['title'])
            job_type_id = self._map_job_type(job_data.get('job_type', ''))
            
            cursor.execute("""
                INSERT INTO JobListings (
                    company_id, job_title, job_description, posting_url, 
                    source_job_board, date_posted, posting_id, scraping_hash, 
                    function, job_type_id, minimum_salary, maximum_salary,
                    pay_frequency, approved, job_status_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         (SELECT id FROM JobStatus WHERE name = 'Active'))
                RETURNING id
            """, (
                self.company_id, job_data['title'], job_data['description'], job_data['url'],
                'BancFirst ADP', job_data['date_posted'], job_data.get('posting_id'),
                job_data['scraping_hash'], function, job_type_id,
                job_data.get('minimum_salary'), job_data.get('maximum_salary'),
                job_data.get('pay_frequency'), True
            ))
            
            result = cursor.fetchone()
            job_id = result['id']
            logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
            return job_id
    
    def _map_job_to_function(self, job_title: str) -> Optional[int]:
        """Map job title to function ID using banking keywords"""
        job_title_lower = job_title.lower()
        
        function_keywords = {
            'Finance': ['finance', 'financial', 'treasury', 'controller', 'audit', 'loan', 'credit', 'banking', 'loan officer', 'mortgage', 'commercial lending', 'credit analyst', 'underwriter'],
            'Customer Service': ['customer service', 'support', 'teller', 'banker', 'representative', 'relationship', 'customer'],
            'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'manager', 'director', 'supervisor', 'lead', 'executive', 'president', 'vice president', 'branch manager'],
            'Information Technology': ['software', 'developer', 'programmer', 'engineer', 'data', 'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud', 'application', 'web', 'mobile', 'qa', 'testing', 'it'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 'compliance officer'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Accounting': ['accounting', 'accountant', 'bookkeeping', 'clerk', 'accounting clerk'],
            'Security': ['security', 'safety', 'guard', 'protection']
        }
        
        for function_name, keywords in function_keywords.items():
            for keyword in keywords:
                if keyword in job_title_lower:
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM Functions WHERE name = %s", (function_name,))
                        result = cursor.fetchone()
                        if result:
                            return result['id']
        
        # Default to 'Other'
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM Functions WHERE name = %s", ('Other',))
            result = cursor.fetchone()
            if result:
                return result['id']
        return None
    
    def _map_job_type(self, work_level_code: str) -> Optional[int]:
        """Map work level to job_type_id"""
        if not work_level_code:
            return None
            
        work_level_lower = work_level_code.lower()
        job_type_mappings = {
            'Full Time': ['full time', 'full-time'],
            'Part Time': ['part time', 'part-time'],
            'Contract': ['contract', 'contractor'],
            'Temporary': ['temporary', 'temp'],
            'Internship': ['intern', 'internship']
        }
        
        for job_type_name, keywords in job_type_mappings.items():
            for keyword in keywords:
                if keyword in work_level_lower:
                    with self.conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM JobType WHERE name LIKE %s", (f"%{job_type_name}%",))
                        result = cursor.fetchone()
                        if result:
                            return result['id']
        return None
    
    def update_company_scrape_completed(self):
        """Update last_full_scrape_completed timestamp"""
        with self.conn.cursor() as cursor:
            cursor.execute("UPDATE Company SET last_full_scrape_completed = CURRENT_TIMESTAMP WHERE id = %s", (self.company_id,))
    
    def mark_stale_jobs_closed(self):
        """Mark old jobs as closed"""
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT last_full_scrape_completed FROM Company WHERE id = %s", (self.company_id,))
            company_data = cursor.fetchone()
            if not company_data or not company_data['last_full_scrape_completed']:
                return
            
            last_scrape_date = company_data['last_full_scrape_completed']
            cursor.execute("""
                UPDATE JobListings SET job_status_id = 6, date_closed = CURRENT_DATE
                WHERE company_id = %s AND job_status_id != 6 AND updated_at < %s
            """, (self.company_id, last_scrape_date))
            
            if cursor.rowcount > 0:
                logger.info(f"Marked {cursor.rowcount} stale jobs as closed")

class BancFirstJobScraper:
    """BancFirst Corporation ADP job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.driver = None
        self.session = requests.Session()
        
        self.config = {
            'jobboard_url': 'https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=1da3e70c-e60a-466c-a367-419990b1b80f&ccId=19000101_000001&type=MP&lang=en_US',
            'api_endpoint': 'https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions',
            'cid': '1da3e70c-e60a-466c-a367-419990b1b80f',
            'ccId': '19000101_000001'
        }
        
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json'
        })
        
        self.setup_selenium()
    
    def setup_selenium(self):
        """Initialize Chrome WebDriver"""
        try:
            chrome_options = Options()
            chrome_options.add_argument('--headless=new')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--window-size=1280,720')
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            chrome_options.page_load_strategy = 'eager'
            
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(15)
            logger.info("Chrome WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def extract_all_jobs_from_dom(self) -> List[Dict]:
        """Extract all jobs from DOM using View All button + scrolling"""
        try:
            logger.info("Loading job listings page...")
            self.driver.get(self.config['jobboard_url'])
            
            wait = WebDriverWait(self.driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(5)
            
            # Click View All button
            try:
                view_all_button = self.driver.find_element(By.ID, "recruitment_careerCenter_showAllJobs")
                self.driver.execute_script("arguments[0].scrollIntoView(true);", view_all_button)
                time.sleep(2)
                self.driver.execute_script("arguments[0].click();", view_all_button)
                time.sleep(8)
                logger.info("✓ Clicked View All button")
            except Exception as e:
                logger.warning(f"Could not click View All button: {e}")
            
            # Scroll to load all jobs
            last_job_count = 0
            scroll_attempts = 0
            
            for scroll in range(25):
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(4)
                
                job_elements = self.driver.find_elements(By.CSS_SELECTOR, "sdf-link[id*='lblTitle_']")
                current_job_count = len(job_elements)
                
                logger.info(f"Scroll {scroll + 1}: Found {current_job_count} job elements")
                
                if current_job_count == last_job_count:
                    scroll_attempts += 1
                    if scroll_attempts >= 5:
                        break
                else:
                    scroll_attempts = 0
                    last_job_count = current_job_count
            
            # Extract job data
            job_links = self.driver.find_elements(By.CSS_SELECTOR, "sdf-link[id*='lblTitle_']")
            jobs_found = []
            
            logger.info(f"Extracting data from {len(job_links)} jobs...")
            
            for i, link in enumerate(job_links):
                try:
                    title = link.text.strip()
                    link_id = link.get_attribute('id')
                    external_job_id = link_id.replace('lblTitle_', '') if 'lblTitle_' in link_id else None
                    
                    location = ""
                    try:
                        parent = link.find_element(By.XPATH, "./ancestor::div[contains(@class, 'current-openings-details')]")
                        location_elem = parent.find_element(By.CSS_SELECTOR, ".current-opening-location-item span")
                        location = location_elem.text.strip()
                    except:
                        pass
                    
                    if title and external_job_id:
                        jobs_found.append({
                            'title': title,
                            'external_job_id': external_job_id,
                            'location': location
                        })
                        logger.info(f"  Job {i+1}: {title} | Location: {location}")
                
                except Exception as e:
                    logger.warning(f"Error extracting job {i+1}: {e}")
            
            logger.info(f"Successfully extracted {len(jobs_found)} jobs from DOM")
            return jobs_found
            
        except Exception as e:
            logger.error(f"Error extracting jobs from DOM: {e}")
            return []
    
    def get_api_data_for_jobs(self) -> Dict[str, Dict]:
        """Get API data for all jobs using pagination"""
        try:
            logger.info("Fetching API data for all jobs with pagination...")
        
            api_job_data = {}
            limit = 20
            offset = 0
            total_retrieved = 0
        
            while True:
                logger.info(f"Fetching API data with offset: {offset}")
            
                timestamp = int(time.time() * 1000)
                params = {
                    'cid': self.config['cid'],
                    'timeStamp': timestamp,
                    'ccId': self.config['ccId'],
                    'lang': 'en_US',
                    'locale': 'en_US',
                    '$top': limit,
                    '$skip': offset
                }
            
                response = self.session.get(
                    self.config['api_endpoint'],
                    params=params,
                    headers={'Referer': self.config['jobboard_url']}
                )
            
                response.raise_for_status()
                data = response.json()
            
                if 'jobRequisitions' not in data:
                    logger.warning("No jobRequisitions in API response")
                    break
            
                batch_jobs = data['jobRequisitions']
                logger.info(f"Retrieved {len(batch_jobs)} jobs in this batch")
            
                if len(batch_jobs) == 0:
                    logger.info("No more jobs returned, pagination complete")
                    break
            
                # Process this batch
                for job in batch_jobs:
                    # Get external job ID
                    external_job_id = None
                    string_fields = job.get('customFieldGroup', {}).get('stringFields', [])
                    for field in string_fields:
                        if field.get('nameCode', {}).get('codeValue') == 'ExternalJobID':
                            external_job_id = field.get('stringValue')
                            break
                
                    if external_job_id:
                        # Extract posting_id
                        posting_id = job.get('clientRequisitionID')
                    
                        # Extract date_posted from dateFields
                        date_posted = None
                        date_fields = job.get('customFieldGroup', {}).get('dateFields', [])
                        for field in date_fields:
                            if field.get('nameCode', {}).get('codeValue') == 'PostingDate':
                                date_value = field.get('dateValue')
                                if date_value:
                                    try:
                                        date_posted = datetime.fromisoformat(date_value.replace('Z', '+00:00'))
                                    except Exception as e:
                                        logger.warning(f"Could not parse date {date_value}: {e}")
                                break
                    
                        # Extract salary info
                        min_salary = max_salary = None
                        pay_grade_range = job.get('payGradeRange', {})
                        if pay_grade_range:
                            min_rate = pay_grade_range.get('minimumRate', {})
                            max_rate = pay_grade_range.get('maximumRate', {})
                            if min_rate and 'amountValue' in min_rate:
                                min_salary = min_rate['amountValue']
                            if max_rate and 'amountValue' in max_rate:
                                max_salary = max_rate['amountValue']
                    
                        api_job_data[external_job_id] = {
                            'posting_id': posting_id,
                            'date_posted': date_posted,
                            'minimum_salary': min_salary,
                            'maximum_salary': max_salary,
                            'work_level': job.get('workLevelCode', {}).get('shortName', '')
                        }
                    
                        logger.debug(f"  API data for {external_job_id}: posting_id={posting_id}, date={date_posted}")
            
                total_retrieved += len(batch_jobs)
            
                # If we got fewer jobs than requested, we're probably done
                if len(batch_jobs) < limit:
                    logger.info("Received fewer jobs than requested, likely at end")
                    break
            
                offset += limit
                time.sleep(0.5)  # Be respectful between requests
        
            logger.info(f"Retrieved API data for {len(api_job_data)} total jobs across all pages")
            return api_job_data
        
        except Exception as e:
            logger.error(f"Error fetching API data: {e}")
            return {}
    
    def filter_target_locations(self, jobs: List[Dict]) -> List[Dict]:
        """Filter for Tulsa, Sand Springs, Coweta"""
        target_locations = ['tulsa', 'sand springs', 'coweta']
        filtered = []
        
        for job in jobs:
            location_text = job.get('location', '').lower()
            for target in target_locations:
                if target in location_text:
                    filtered.append(job)
                    logger.info(f"  ✓ Target job: {job['title']} at {job['location']}")
                    break
        
        logger.info(f"Found {len(filtered)} target location jobs")
        return filtered
    
    def scrape_job_description(self, external_job_id: str) -> str:
        """Scrape individual job description"""
        job_url = f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={self.config['cid']}&ccId={self.config['ccId']}&type=MP&lang=en_US&selectedMenuKey=CareerCenter&jobId={external_job_id}"
        
        try:
            self.driver.get(job_url)
            time.sleep(2)
            
            soup = BeautifulSoup(self.driver.page_source, 'html.parser')
            body = soup.find('body')
            if body:
                for tag in body.find_all(['script', 'style', 'nav', 'header', 'footer']):
                    tag.decompose()
                description = body.get_text(strip=True)
                logger.info(f"  Extracted description: {len(description)} chars")
                return description
        except Exception as e:
            logger.warning(f"Error scraping job description: {e}")
        
        return ""
    
    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}
        
        try:
            # Step 1: Extract all jobs from DOM
            all_jobs = self.extract_all_jobs_from_dom()
            if not all_jobs:
                raise Exception("No jobs found in DOM")
            
            # Step 2: Get API data
            api_data = self.get_api_data_for_jobs()
            
            # Step 3: Filter target locations
            target_jobs = self.filter_target_locations(all_jobs)
            stats['found'] = len(target_jobs)
            
            # Step 4: Process each job
            for i, job in enumerate(target_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(target_jobs)}: {job['title']}")
                    
                    external_job_id = job['external_job_id']
                    job_url = f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={self.config['cid']}&ccId={self.config['ccId']}&type=MP&lang=en_US&selectedMenuKey=CareerCenter&jobId={external_job_id}"
                    
                    # Check if job exists
                    if self.db.check_existing_job(job_url):
                        stats['updated'] += 1
                        continue
                    
                    # Get job description
                    description = self.scrape_job_description(external_job_id)
                    if len(description.strip()) < 50:
                        logger.warning("  Insufficient job description")
                        stats['skipped'] += 1
                        continue
                    
                    # Get API data for this job
                    job_api_data = api_data.get(external_job_id, {})
                    
                    # Prepare job data
                    job_data = {
                        'title': job['title'],
                        'url': job_url,
                        'description': description,
                        'date_posted': job_api_data.get('date_posted'),
                        'posting_id': job_api_data.get('posting_id'),
                        'job_type': job_api_data.get('work_level', ''),
                        'minimum_salary': job_api_data.get('minimum_salary'),
                        'maximum_salary': job_api_data.get('maximum_salary'),
                        'scraping_hash': hashlib.md5(f"{job['title']}{job_url}{description}".encode()).hexdigest()
                    }
                    
                    logger.info(f"  Storing job with posting_id={job_data['posting_id']}, date={job_data['date_posted']}")
                    
                    # Store in database
                    self.db.store_job_listing(job_data)
                    stats['added'] += 1
                    time.sleep(1)
                    
                except Exception as e:
                    error_msg = f"Error processing {job['title']}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Cleanup
            self.db.mark_stale_jobs_closed()
            self.db.update_company_scrape_completed()
            
        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats
    
    def cleanup(self):
        """Clean up resources"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("WebDriver closed")
            except:
                pass

def main():
    """Main execution function"""
    db_password = os.getenv('POSTGRES_PASSWORD')
    if not db_password:
        logger.error("Please set POSTGRES_PASSWORD environment variable")
        return 1
    
    db_connection = f"postgresql://postgres:{db_password}@localhost:5432/tulsa_jobs"
    
    scraper = None
    try:
        db_manager = DatabaseManager(db_connection)
        scraper = BancFirstJobScraper(db_manager)
        
        logger.info("Starting BancFirst job scraping...")
        results = scraper.scrape_jobs()
        
        logger.info("=== SCRAPING SUMMARY ===")
        logger.info(f"Jobs found: {results['found']}")
        logger.info(f"Jobs added: {results['added']}")
        logger.info(f"Jobs updated: {results['updated']}")
        logger.info(f"Jobs skipped: {results['skipped']}")
        logger.info(f"Errors: {len(results['errors'])}")
        
        if results['errors']:
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
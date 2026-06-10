#!/usr/bin/env python3
"""
Complete Clifford Power Systems Paycom Job Scraper with Selenium
Handles JavaScript-heavy Single Page Applications
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
        logging.FileHandler('clifford_scraper.log', encoding='utf-8'),
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
                1,
                True
            ))
            
            result = cursor.fetchone()
            company_id = result['id']
            logger.info(f"Created new company: {company_data['name']} (ID: {company_id})")
            return company_id
    
    def get_job_type_id(self, position_type: str) -> Optional[int]:
        """Get job type ID based on position type"""
        with self.conn.cursor() as cursor:
            if position_type == 'contract':
                cursor.execute("SELECT id FROM jobtype WHERE id = 1")  # Contract (1099)
            else:
                cursor.execute("SELECT id FROM jobtype WHERE id = 2")  # Full-time (assume default)
            
            result = cursor.fetchone()
            return result['id'] if result else None
    
    def store_job_listing(self, job_data: Dict, company_id: int, position_type: str) -> int:
        """Store or update job listing, return job listing ID"""
        with self.conn.cursor() as cursor:
            # Check for existing job by URL and title+company
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s 
                OR (job_title = %s AND company_id = %s)
            """, (job_data['url'], job_data['title'], company_id))
            
            existing = cursor.fetchone()
            
            # Get job type ID
            job_type_id = self.get_job_type_id(position_type)
            
            if existing:
                # Update existing job
                cursor.execute("""
                    UPDATE JobListings SET
                        job_title = %s,
                        job_description = %s,
                        posting_url = %s,
                        date_posted = %s,
                        scraping_hash = %s,
                        job_type_id = %s,
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
                        source_job_board, date_posted, scraping_hash, 
                        job_type_id, approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Clifford Power Paycom',
                    job_data['date_posted'],
                    job_data['scraping_hash'],
                    job_type_id,
                    True
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"Created new job: {job_data['title']} (ID: {job_id})")
                return job_id
    
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
            chrome_options.add_argument('--disable-images')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--window-size=1280,720')
            chrome_options.add_argument('--log-level=3')
            chrome_options.add_argument('--silent')
            
            chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
            
            # Set page load strategy to eager
            chrome_options.page_load_strategy = 'eager'
            
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except:
                self.driver = webdriver.Chrome('./chromedriver.exe', options=chrome_options)
            
            self.driver.implicitly_wait(5)
            self.driver.set_page_load_timeout(15)
            self.driver.set_script_timeout(10)
            
            # Execute script to remove automation detection
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            logger.info("Optimized Selenium WebDriver initialized")
            
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise
    
    def get_job_listings(self, base_url: str) -> List[Dict]:
        """Get all job listings from Paycom job board with pagination"""
        all_jobs = []
        page_num = 1
        
        try:
            logger.info(f"Loading Paycom job board...")
            self.driver.get(base_url)
            
            # Wait for page to load
            wait = WebDriverWait(self.driver, 15)
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(3)
            
            while True:
                logger.info(f"Processing page {page_num}...")
                
                # Find job listings on current page
                page_jobs = self._extract_jobs_from_current_page()
                
                if not page_jobs:
                    logger.warning(f"No jobs found on page {page_num}")
                    break
                
                all_jobs.extend(page_jobs)
                logger.info(f"Found {len(page_jobs)} jobs on page {page_num} (total: {len(all_jobs)})")
                
                # Try to go to next page
                try:
                    # First check if any next button exists
                    next_buttons = self.driver.find_elements(By.CSS_SELECTOR, 'a.js-pagination-link-next')
                    if next_buttons:
                        next_button = next_buttons[0]
                        # Check if it's not disabled
                        if next_button.get_attribute('aria-disabled') != 'true':
                            logger.info(f"Going to next page...")
                            next_button.click()
                            time.sleep(2)  # Wait for page to load
                            page_num += 1
                        else:
                            logger.info("Next button is disabled - reached last page")
                            break
                    else:
                        logger.info("No next button found")
                        break
                except Exception as e:
                    logger.warning(f"Error during pagination: {e}")
                    break
            
            logger.info(f"Extracted {len(all_jobs)} total jobs from {page_num} pages")
            return all_jobs
            
        except Exception as e:
            logger.error(f"Error getting job listings: {e}")
            return all_jobs
    
    def _extract_jobs_from_current_page(self) -> List[Dict]:
        """Extract jobs from current page and filter for Tulsa"""
        jobs = []
        
        try:
            # Look for job containers that include location info
            job_containers = []
            
            # Try different container selectors
            container_selectors = [
                '.JobListing',
                '.job-listing',
                '[class*="job"]',
                'tr',  # Table rows
                'div[onclick*="ViewJobDetails"]'
            ]
            
            for selector in container_selectors:
                try:
                    containers = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if containers:
                        job_containers = containers
                        logger.info(f"Found {len(job_containers)} job containers using selector: {selector}")
                        break
                except:
                    continue
            
            if not job_containers:
                logger.warning("No job containers found")
                return []
            
            for container in job_containers:
                try:
                    # Look for job title link
                    job_link = None
                    job_title = ""
                    
                    # Try to find the job link
                    link_selectors = [
                        'a[href*="ViewJobDetails"]',
                        'a[onclick*="ViewJobDetails"]'
                    ]
                    
                    for link_sel in link_selectors:
                        try:
                            link_element = container.find_element(By.CSS_SELECTOR, link_sel)
                            if link_element:
                                job_link = link_element.get_attribute('href')
                                job_title = link_element.text.strip()
                                break
                        except:
                            continue
                    
                    # If no href, check onclick for job ID
                    if not job_link:
                        try:
                            onclick = container.get_attribute('onclick')
                            if onclick and 'ViewJobDetails' in onclick:
                                # Extract job ID from onclick
                                import re
                                job_match = re.search(r'job=(\d+)', onclick)
                                client_match = re.search(r'clientkey=([A-F0-9]+)', onclick)
                                if job_match and client_match:
                                    job_id = job_match.group(1)
                                    client_key = client_match.group(1)
                                    job_link = f"https://www.paycomonline.net/v4/ats/web.php/jobs/ViewJobDetails?job={job_id}&clientkey={client_key}"
                        except:
                            pass
                    
                    if not job_link or not job_title:
                        continue
                    
                    # Look for location information in the same container
                    location_text = ""
                    
                    # Try different location selectors
                    location_selectors = [
                        '.jobLocation',
                        '.jobInfoLine.jobLocation',
                        '[class*="location"]',
                        'span[onclick*="ViewJobDetails"]'
                    ]
                    
                    for loc_sel in location_selectors:
                        try:
                            location_element = container.find_element(By.CSS_SELECTOR, loc_sel)
                            if location_element:
                                location_text = location_element.text.strip()
                                break
                        except:
                            continue
                    
                    # If no specific location element, check all text in container
                    if not location_text:
                        location_text = container.text
                    
                    # Check if this job is in Tulsa
                    if 'tulsa' in location_text.lower():
                        logger.info(f"  ✓ Found Tulsa job: {job_title}")
                        jobs.append({
                            'title': job_title,
                            'url': job_link,
                            'location': location_text
                        })
                    else:
                        logger.debug(f"  ✗ Non-Tulsa job: {job_title} - {location_text}")
                
                except Exception as e:
                    logger.debug(f"Error processing job container: {e}")
                    continue
            
            return jobs
            
        except Exception as e:
            logger.error(f"Error extracting jobs from current page: {e}")
            return []
    
    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)
            
            wait = WebDriverWait(self.driver, timeout)
            
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning(f"  Body tag not found within timeout")
                return ""
            
            time.sleep(1.5)
            
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

class CliffordScraperWithSelenium:
    """Clifford Power Systems scraper that uses Selenium for JavaScript-heavy job pages"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'Clifford Power Systems',
            'website': 'https://cliffordpower.com',
            'jobboard_url': 'https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=2A518A736F79A1B70B942BDFB12C29FF&jpt=',
            'location_filters': ['Tulsa']
        }
        
        # Set up session headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1'
        })
    
    def get_job_content_for_tulsa_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Get detailed content for jobs already filtered for Tulsa"""
        logger.info(f"Getting detailed content for {len(jobs)} Tulsa jobs...")
        
        # Add content to each job by scraping individual pages
        for i, job in enumerate(jobs):
            try:
                logger.info(f"Getting content for job {i+1}/{len(jobs)}: {job.get('title', 'Unknown')}")
                
                # Download job details with Selenium
                job_html = self.selenium_scraper.get_job_content(job['url'])
                if not job_html or len(job_html.strip()) < 100:
                    logger.warning(f"  Failed to get job content")
                    job['html_content'] = ""
                    job['text_content'] = ""
                    continue
                
                # Extract text content
                job_text = self.extract_job_content(job_html)
                
                job['html_content'] = job_html
                job['text_content'] = job_text.lower()
                
                time.sleep(1)  # Be respectful
                
            except Exception as e:
                logger.error(f"Error getting content for job {job.get('title', 'Unknown')}: {e}")
                job['html_content'] = ""
                job['text_content'] = ""
                continue
        
        return jobs
    
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
    
    def extract_job_content(self, html_content: str) -> str:
        """Extract job content from HTML - only content between body and footer"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Find the body tag
            body = soup.find('body')
            if not body:
                logger.warning("No body tag found")
                return html_content
            
            # Remove footer and everything after it
            footer = body.find(class_=lambda x: x and 'footer' in x.lower())
            if footer:
                # Remove footer and all following siblings
                for sibling in footer.find_next_siblings():
                    sibling.decompose()
                footer.decompose()
                logger.info("Removed footer and following content")
            
            # Remove scripts, styles, navigation from the body content
            for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header']):
                tag.decompose()
            
            # Return the cleaned body HTML
            body_html = str(body)
            logger.info(f"Extracted trimmed content: {len(body_html)} characters")
            return body_html
            
        except Exception as e:
            logger.warning(f"Error extracting job content: {e}")
            return html_content
    
    def extract_salary_info(self, text: str) -> Dict:
        """Extract salary information using regex patterns"""
        salary_data = {
            'min_salary': None,
            'max_salary': None,
            'salary_type': None
        }
        
        # Clean text for easier matching
        text = re.sub(r'\s+', ' ', text.lower())
        
        # Salary patterns
        patterns = [
            # $50,000 - $75,000 per year
            r'\$([0-9,.]+)\s*[-–]\s*\$([0-9,.]+)\s*(?:per\s+year|annually|/year)',
            # $50,000-$75,000
            r'\$([0-9,.]+)\s*[-–]\s*\$([0-9,.]+)',
            # $25-$35 per hour
            r'\$([0-9.]+)\s*[-–]\s*\$([0-9.]+)\s*(?:per\s+hour|/hour|hourly)',
            # Salary range: $50,000 to $75,000
            r'salary.*?\$([0-9,.]+)\s*(?:to|-)\s*\$([0-9,.]+)',
            # Single salary: $65,000
            r'\$([0-9,.]+)\s*(?:per\s+year|annually|/year)',
            # Hourly: $25/hour
            r'\$([0-9.]+)\s*(?:per\s+hour|/hour|hourly)'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text)
            if matches:
                match = matches[0]
                if len(match) == 2:  # Range
                    min_val = float(re.sub(r'[,$]', '', match[0]))
                    max_val = float(re.sub(r'[,$]', '', match[1]))
                    
                    # Determine if hourly or annual
                    if 'hour' in pattern:
                        salary_data['salary_type'] = 'hourly'
                        # Convert hourly to annual (assume 2080 hours/year)
                        if min_val < 100:  # Likely hourly rate
                            min_val *= 2080
                            max_val *= 2080
                    else:
                        salary_data['salary_type'] = 'annual'
                    
                    salary_data['min_salary'] = int(min_val)
                    salary_data['max_salary'] = int(max_val)
                    break
                else:  # Single value
                    val = float(re.sub(r'[,$]', '', match[0]))
                    
                    if 'hour' in pattern:
                        salary_data['salary_type'] = 'hourly'
                        if val < 100:  # Convert hourly to annual
                            val *= 2080
                    else:
                        salary_data['salary_type'] = 'annual'
                    
                    salary_data['min_salary'] = int(val)
                    salary_data['max_salary'] = int(val)
                    break
        
        # Check for competitive/DOE
        if 'competitive' in text:
            salary_data['salary_type'] = 'competitive'
        elif 'doe' in text or 'depends on experience' in text:
            salary_data['salary_type'] = 'doe'
        
        return salary_data
    
    def extract_position_type(self, text: str) -> str:
        """Extract position type (full-time, part-time, contract)"""
        text = text.lower()
        
        if 'part-time' in text or 'part time' in text:
            return 'part_time'
        elif 'contract' in text or 'contractor' in text or 'temporary' in text:
            return 'contract'
        elif 'full-time' in text or 'full time' in text:
            return 'full_time'
        else:
            return 'full_time'  # Default assumption
    
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
            
            # Step 2: Get Tulsa job listings from Paycom (filtered on main page)
            logger.info("Step 2: Getting Tulsa job listings from Paycom...")
            tulsa_jobs = self.selenium_scraper.get_job_listings(self.company_config['jobboard_url'])
            if not tulsa_jobs:
                raise Exception("No Tulsa jobs retrieved from Paycom")
            logger.info(f"✓ Retrieved {len(tulsa_jobs)} Tulsa jobs from Paycom (filtered on main page)")
            
            # Step 3: Get detailed content for each Tulsa job
            logger.info("Step 3: Getting detailed content for Tulsa jobs...")
            tulsa_jobs = self.get_job_content_for_tulsa_jobs(tulsa_jobs)
            stats['found'] = len(tulsa_jobs)
            
            if len(tulsa_jobs) == 0:
                logger.warning("No jobs found after filtering")
                return stats
            
            # Step 4: Process each job
            for i, job in enumerate(tulsa_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(tulsa_jobs)}: {job.get('title', 'Unknown')}")
                    
                    # Get job content (already retrieved during filtering)
                    job_html = job.get('html_content', '')
                    job_text = job.get('text_content', '')
                    
                    if not job_html:
                        logger.warning(f"  No job content available")
                        stats['skipped'] += 1
                        continue
                    
                    # Extract salary and position type
                    salary_data = self.extract_salary_info(job_text)
                    position_type = self.extract_position_type(job_text)
                    
                    # Log extracted information
                    logger.info(f"  Job Title: {job['title']}")
                    logger.info(f"  Job URL: {job['url']}")
                    if salary_data['min_salary'] or salary_data['max_salary']:
                        logger.info(f"  Salary: ${salary_data.get('min_salary', 'N/A')} - ${salary_data.get('max_salary', 'N/A')} ({salary_data.get('salary_type', 'N/A')})")
                    else:
                        logger.info(f"  Salary: Not found")
                    logger.info(f"  Position Type: {position_type}")
                    
                    # Use the trimmed content for the job description
                    trimmed_html = self.extract_job_content(job_html)
                    
                    # Prepare job data for database
                    job_data = {
                        'title': job.get('title', ''),
                        'url': job['url'],
                        'description': trimmed_html,  # Use trimmed content
                        'date_posted': datetime.now(),
                        'scraping_hash': self.create_scraping_hash({
                            'title': job.get('title', ''),
                            'url': job['url'],
                            'description': trimmed_html
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id, position_type)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    
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
            self.db.log_scraping_activity('Clifford Power Paycom', stats)
            
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
        scraper = CliffordScraperWithSelenium(db_manager)
        
        # Run scraping
        logger.info("Starting Clifford Power Systems job scraping with Selenium...")
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
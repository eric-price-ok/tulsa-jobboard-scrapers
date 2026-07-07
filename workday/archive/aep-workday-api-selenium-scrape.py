#!/usr/bin/env python3
"""
aep-workday-scrape.py
American Electric Power (AEP) Tulsa Job Scraper
Uses AEP's Tulsa-specific URL for accurate filtering
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
        logging.FileHandler('aep_scraper.log', encoding='utf-8'),
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
                    'AEP Tulsa',
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
    
    def _map_category_to_function(self, category: str) -> Optional[int]:
        """Map AEP category to function ID"""
        with self.conn.cursor() as cursor:
            # Try exact match first
            cursor.execute("SELECT id FROM Functions WHERE name = %s", (category,))
            result = cursor.fetchone()
            if result:
                logger.info(f"  Mapped category '{category}' to function: {category}")
                return result['id']
            
            # Try partial matches for common AEP categories
            category_lower = category.lower()
            category_mappings = {
                'environmental': 'Environmental',
                'information technology': 'Information Technology',
                'engineering': 'Engineering, Electrical',  # Default to electrical for AEP
                'finance': 'Finance',
                'human resources': 'Human Resources',
                'legal': 'Legal',
                'operations': 'Project Management',
                'maintenance': 'Skilled Labor',
                'safety': 'Security',
                'customer': 'Customer Service',
                'administrative': 'Administration'
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
        """Map job title to function ID using keywords - AEP specific mappings"""
        job_title_lower = job_title.lower()
        
        # Define function mapping keywords (enhanced for utility company roles)
        function_keywords = {
            'Information Technology': [
                'software', 'developer', 'programmer', 'engineer', 'tech', 'it ', 'data', 
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'cyber'
            ],
            'Engineering, Electrical': [
                'electrical', 'elec eng', 'electrical engineer', 'power engineer', 
                'transmission', 'distribution', 'substation', 'relay', 'protection'
            ],
            'Engineering, Mechanical': ['mechanical', 'mech eng', 'mechanical engineer'],
            'Engineering, Civil': ['civil', 'civil engineer'],
            'Project Management': [
                'operations', 'ops', 'plant', 'facility', 'power plant', 'generation',
                'operator', 'control room', 'dispatch', 'grid', 'utility', 'maintenance',
                'project manager', 'program manager', 'scrum master', 'project coordinator'
            ],
            'Skilled Labor': [
                'lineman', 'lineworker', 'technician', 'maintenance', 'mechanic', 
                'welder', 'electrician', 'apprentice', 'journeyman', 'crew'
            ],
            'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue', 'customer'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract', 'regulatory'],
            'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client'],
            'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
            'Quality': ['quality', 'qa', 'qc', 'testing', 'inspector', 'assurance'],
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
    
    def get_job_listings_from_page(self, url: str, timeout=15) -> List[Dict]:
        """Load AEP Tulsa jobs page and extract job listings"""
        try:
            logger.info(f"Loading AEP Tulsa jobs page: {url}")
            self.driver.get(url)
            
            wait = WebDriverWait(self.driver, timeout)
            
            # Wait for job listings to load
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(2)  # Give time for dynamic content to load
            except TimeoutException:
                logger.warning(f"Timeout waiting for page to load")
                return []
            
            # Extract job listings from the page
            jobs = []
            
            # Look for job listing elements (will need to inspect the page to find exact selectors)
            job_selectors = [
                '.job-item',
                '.job-listing',
                '[data-automation-id="job_title"]',
                'a[href*="job"]',
                '.position-title'
            ]
            
            for selector in job_selectors:
                job_elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if job_elements:
                    logger.info(f"Found {len(job_elements)} job elements using selector: {selector}")
                    
                    for element in job_elements:
                        try:
                            # Extract job information
                            title = element.text.strip()
                            href = element.get_attribute('href')
                            
                            if title and href and 'job' in href.lower():
                                jobs.append({
                                    'title': title,
                                    'url': href,
                                    'source': 'AEP Tulsa Page'
                                })
                        except Exception as e:
                            logger.warning(f"Error extracting job from element: {e}")
                    
                    break  # Found jobs with this selector, no need to try others
            
            # If no specific job selectors work, try to find all links that might be jobs
            if not jobs:
                logger.info("Trying to find job links in page...")
                all_links = self.driver.find_elements(By.TAG_NAME, "a")
                
                for link in all_links:
                    try:
                        href = link.get_attribute('href')
                        text = link.text.strip()
                        
                        if href and text and ('job' in href.lower() or 'position' in href.lower()):
                            jobs.append({
                                'title': text,
                                'url': href,
                                'source': 'AEP Tulsa Page'
                            })
                    except:
                        continue
            
            logger.info(f"Extracted {len(jobs)} jobs from page")
            return jobs
            
        except Exception as e:
            logger.error(f"Error loading jobs page: {e}")
            return []
    
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

class AEPTulsaScraper:
    """AEP Tulsa scraper using the Tulsa-specific URL"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        
        # AEP-specific configuration
        self.company_config = {
            'name': 'American Electric Power',
            'website': 'https://aep.com',
            'jobboard_url': 'https://www.aep.com/careers/positions/?&location=tulsa,%20ok',
            'base_tulsa_url': 'https://www.aep.com/careers/positions/?&location=tulsa,%20ok&pageNumber={}'
        }
    
    def get_all_tulsa_jobs(self) -> List[Dict]:
        """Get all Tulsa jobs by checking multiple pages if needed"""
        all_jobs = []
        page_number = 1
        max_pages = 10  # Safety limit
        
        while page_number <= max_pages:
            logger.info(f"Checking page {page_number} for Tulsa jobs...")
            
            page_url = self.company_config['base_tulsa_url'].format(page_number)
            page_jobs = self.selenium_scraper.get_job_listings_from_page(page_url)
            
            if not page_jobs:
                logger.info(f"No jobs found on page {page_number}, stopping pagination")
                break
            
            all_jobs.extend(page_jobs)
            logger.info(f"Found {len(page_jobs)} jobs on page {page_number}")
            
            page_number += 1
            time.sleep(1)  # Be respectful between page loads
        
        logger.info(f"Total Tulsa jobs found across all pages: {len(all_jobs)}")
        return all_jobs
    
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
                'maximum_salary': None
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
                                extracted_fields['date_posted'] = parse_workday_date(some_date_string)
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
            
            # Extract category: <span class="fw-bold">Category</span><br/>Environmental Services
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
                        import re
                        date_match = re.search(r'Job Posting End Date.*?(\d{1,2}-\d{1,2}-\d{4})', text_after)
                        if date_match:
                            date_text = date_match.group(1)
                            extracted_fields['date_closed'] = self.parse_date(date_text)
                            logger.info(f"  Extracted date closed: {date_text}")
                            break
            except Exception as e:
                logger.warning(f"  Could not extract date closed: {e}")
            
            # Extract salary range: $112,869.00-146,730.50 USD
            try:
                # Look for "Compensation Range:" text
                comp_elements = soup.find_all(string=re.compile(r'Compensation Range:', re.IGNORECASE))
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
            # Step 1: Get company ID
            logger.info("Step 1: Getting/creating company...")
            company_id = self.db.get_or_create_company(self.company_config)
            logger.info(f"✓ Company ID: {company_id}")
            
            # Step 2: Get all Tulsa job listings from AEP pages
            logger.info("Step 2: Getting Tulsa job listings from AEP...")
            tulsa_jobs = self.get_all_tulsa_jobs()
            if not tulsa_jobs:
                raise Exception("No Tulsa jobs found on AEP website")
            
            stats['found'] = len(tulsa_jobs)
            logger.info(f"✓ Found {len(tulsa_jobs)} Tulsa jobs")
            
            # Step 3: Process each job with Selenium
            for i, job in enumerate(tulsa_jobs):
                try:
                    logger.info(f"Processing job {i+1}/{len(tulsa_jobs)}: {job.get('title', 'Unknown')}")
                    
                    job_url = job.get('url', '')
                    if not job_url:
                        logger.warning(f"  No URL found for job")
                        stats['skipped'] += 1
                        continue
                    
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
                        'date_posted': extracted_fields.get('date_posted') if extracted_fields else None,
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
                    time.sleep(1.0)  # 2 second delay between job page scrapes
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark scrape as completed and close old jobs
            logger.info("Step 4: Marking scrape as completed...")
            self.db.mark_scrape_completed(company_id)
            
            logger.info("Step 5: Marking old jobs as closed...")
            self.db.mark_old_jobs_closed(company_id, scrape_start_time)
            
            # Step 6: Log results
            logger.info("Step 6: Logging results...")
            self.db.log_scraping_activity('AEP Tulsa', stats)
            
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
        scraper = AEPTulsaScraper(db_manager)
        
        # Run scraping
        logger.info("Starting AEP Tulsa job scraping...")
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
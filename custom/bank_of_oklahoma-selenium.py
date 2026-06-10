#!/usr/bin/env python3
"""
Bank of Oklahoma Job Scraper
Scrapes jobs from BOK careers site and stores in tulsa_jobs database
Based on PowerShell script logic but writes to database instead of files
"""

import requests
from bs4 import BeautifulSoup
import psycopg
from psycopg.rows import dict_row
import logging
from typing import Dict, List, Optional
import hashlib
from datetime import datetime, timedelta
import time
import re
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bok_scraper.log', encoding='utf-8'),
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
                        source_job_board, scraping_hash, 
                        function, approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Bank of Oklahoma',
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
                'analyst', 'database', 'system', 'network', 'security', 'devops', 'cloud',
                'application', 'web', 'mobile', 'qa', 'testing', 'scrum', 'agile', 'solutions architect','enterprise architect'
            ],
            'Engineering - Mechanical': ['mechanical', 'mech eng', 'mechanical engineer'],
            'Engineering - Electrical': ['electrical', 'elec eng', 'electrical engineer'],
            'Engineering - Civil': ['civil', 'civil engineer'],
            'Finance': ['finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller', 'audit', 'banking', 'credit', 'loan'],
            'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
            'Sales': ['sales', 'account manager', 'business development', 'bd', 'revenue', 'relationship manager'],
            'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
            'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
            'Operations': ['operations', 'ops', 'supply chain', 'logistics', 'process', 'facility'],
            'Project Management': ['project manager', 'program manager', 'scrum master', 'project coordinator'],
            'Customer Service': ['customer service', 'support', 'help desk', 'call center', 'client', 'banker', 'teller'],
            'Administration': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
            'Security': ['security', 'safety', 'guard', 'protection'],
            'Skilled Labor': ['operator', 'technician', 'maintenance', 'mechanic', 'welder', 'electrician'],
            'Healthcare Provider': ['nurse', 'doctor', 'medical', 'healthcare', 'clinical', 'physician']
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

class BOKJobScraper:
    """Bank of Oklahoma job scraper"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.session = requests.Session()
        
        self.company_config = {
            'name': 'Bank of Oklahoma',
            'website': 'https://www.bokf.com',
            'jobboard_url': 'https://jobs.bokf.com',
            'search_url': 'https://jobs.bokf.com/search/',
            'base_params': {
                'q': '',
                'location': 'Tulsa',
                'sortColumn': 'referencedate',
                'sortDirection': 'desc'
            }
        }
        
        # Set up session headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Sec-GPC': '1',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'same-origin',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache'
        })
    
    def get_job_listings(self) -> List[Dict]:
        """Get all job listings from BOK careers page using pagination"""
        all_jobs = []
        limit = 25
        startrow = 0
        max_iterations = 20
        iteration = 0
        previous_page_urls = set()
        
        while iteration < max_iterations:
            try:
                logger.info(f"Fetching page {iteration + 1} (startrow: {startrow})...")
                
                # Build URL with pagination parameters
                params = self.company_config['base_params'].copy()
                params['startrow'] = startrow
                
                response = self.session.get(
                    self.company_config['search_url'],
                    params=params,
                    headers={
                        'Referer': f"{self.company_config['search_url']}?q=&location=Tulsa"
                    }
                )
                
                response.raise_for_status()
                
                # Parse HTML to extract job listings
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Find job links using the pattern from PowerShell script
                job_links = soup.find_all('a', class_='jobTitle-link')
                
                logger.info(f"Found {len(job_links)} job postings on page {iteration + 1}")
                
                if len(job_links) == 0:
                    logger.info("No job postings found. Ending pagination.")
                    break
                
                # Extract job URLs and titles
                current_page_urls = set()
                page_jobs = []
                
                for link in job_links:
                    job_title = link.get_text(strip=True)
                    job_href = link.get('href')
                    
                    if job_href:
                        # Build full URL
                        if job_href.startswith('/'):
                            job_url = self.company_config['jobboard_url'] + job_href
                        else:
                            job_url = job_href
                        
                        current_page_urls.add(job_url)
                        page_jobs.append({
                            'title': job_title,
                            'url': job_url
                        })
                
                # Check for duplicate URLs from previous page (end of pagination detection)
                if iteration > 0 and previous_page_urls:
                    duplicate_count = len(current_page_urls.intersection(previous_page_urls))
                    logger.info(f"Duplicate jobs from previous page: {duplicate_count} out of {len(current_page_urls)}")
                    
                    # If we have mostly the same jobs, we've reached the end
                    if duplicate_count >= (len(current_page_urls) * 0.8):
                        logger.info("Too many duplicate jobs from previous page. Ending pagination.")
                        break
                
                # Add jobs to results
                all_jobs.extend(page_jobs)
                
                # Store current page URLs for next comparison
                previous_page_urls = current_page_urls.copy()
                
                # Move to next page
                startrow += limit
                iteration += 1
                
                # Be respectful with timing
                time.sleep(3)
                
            except Exception as e:
                logger.error(f"Error fetching page {iteration + 1}: {e}")
                break
        
        # Remove duplicates based on URL
        unique_jobs = []
        seen_urls = set()
        
        for job in all_jobs:
            if job['url'] not in seen_urls:
                unique_jobs.append(job)
                seen_urls.add(job['url'])
        
        logger.info(f"Total unique job postings found: {len(unique_jobs)}")
        return unique_jobs
    
    def download_job_content(self, job_url: str) -> str:
        """Download individual job page content"""
        try:
            logger.info(f"  Downloading job content from: {job_url}")
            
            response = self.session.get(job_url)
            response.raise_for_status()
            
            # Trim content similar to PowerShell script logic
            content = self.trim_html_content(response.text)
            
            logger.info(f"  Downloaded content: {len(content)} characters")
            return content
            
        except Exception as e:
            logger.error(f"  Error downloading job content: {e}")
            return ""
    
    def trim_html_content(self, html_content: str) -> str:
        """Trim HTML content like the PowerShell script"""
        try:
            # Find the position of the first </style> tag
            style_end_index = html_content.lower().find("</style>")
            
            if style_end_index >= 0:
                # Calculate the position after </style> tag (add 8 for the length of "</style>")
                start_position = style_end_index + 8
                html_content = html_content[start_position:]
            else:
                logger.info("  No </style> tag found in content")
            
            # Find the position of the "About BOK Financial Corporation" h4 tag
            about_bok_pattern = r"<h4>About BOK Financial Corporation</h4>"
            about_bok_match = re.search(about_bok_pattern, html_content, re.IGNORECASE)
            
            if about_bok_match:
                # Return the content up to (but not including) the About BOK section
                return html_content[:about_bok_match.start()]
            else:
                logger.info("  No 'About BOK Financial Corporation' section found in content")
                return html_content
                
        except Exception as e:
            logger.warning(f"Error trimming HTML content: {e}")
            return html_content
    
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
            
            # Step 2: Get job listings
            logger.info("Step 2: Getting job listings...")
            job_listings = self.get_job_listings()
            if not job_listings:
                raise Exception("No jobs retrieved")
            
            stats['found'] = len(job_listings)
            logger.info(f"✓ Retrieved {len(job_listings)} jobs")
            
            # Step 3: Process each job
            for i, job in enumerate(job_listings):
                try:
                    logger.info(f"Processing job {i+1}/{len(job_listings)}: {job['title']}")
                    
                    # Download job content
                    job_content = self.download_job_content(job['url'])
                    if not job_content or len(job_content.strip()) < 100:
                        logger.warning(f"  Failed to get meaningful job content")
                        stats['skipped'] += 1
                        continue
                    
                    # Prepare job data for database
                    job_data = {
                        'title': job['title'],
                        'url': job['url'],
                        'description': job_content,
                        'scraping_hash': self.create_scraping_hash({
                            'title': job['title'],
                            'url': job['url'],
                            'description': job_content
                        })
                    }
                    
                    # Store job in database
                    job_id = self.db.store_job_listing(job_data, company_id)
                    logger.info(f"  ✓ Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Mark old jobs as closed
            logger.info("Step 4: Marking old jobs as closed...")
            cutoff_date = datetime.now() - timedelta(days=7)
            self.db.mark_old_jobs_closed(company_id, cutoff_date)
            
            # Step 5: Log results
            logger.info("Step 5: Logging results...")
            self.db.log_scraping_activity('Bank of Oklahoma', stats)
            
        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats

def main():
    """Main execution function"""
    # Get password from environment variable
    db_password = os.getenv('POSTGRES_PASSWORD')
    if not db_password:
        logger.error("Please set POSTGRES_PASSWORD environment variable")
        logger.error("Example: set POSTGRES_PASSWORD=your_password")
        return 1
    
    db_connection = f"postgresql://postgres:{db_password}@localhost:5432/tulsa_jobs"
    
    try:
        # Initialize components
        db_manager = DatabaseManager(db_connection)
        scraper = BOKJobScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Bank of Oklahoma job scraping...")
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
    
    return 0

if __name__ == "__main__":
    exit(main())
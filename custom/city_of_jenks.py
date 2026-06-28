#!/usr/bin/env python3
"""
jenks-jobs-scraper.py
City of Jenks Job Board Scraper
Scrapes job listings and extracts PDF content to text
"""

import requests
import PyPDF2
import io
import time
from bs4 import BeautifulSoup
from utils.posting_operations import store_job_listing, load_active_jobs_cache, check_job_in_cache, update_job_verified_timestamp, mark_stale_jobs_closed
from utils.utility_methods import setup_logging
from utils.company_operations import get_company_config_by_name
from utils.db_connection import get_database_connection
from utils.location_utilities import get_city_id
from datetime import datetime
import logging
from typing import Dict, List, Optional

class DatabaseManager:
    """Handles all PostgreSQL database operations"""
    
    def __init__(self):
        self.conn = get_database_connection()
        self.active_jobs_cache = {}

    def load_active_jobs_cache(self, company_id: int):
        """Load and cache all active jobs for the company"""
        with self.conn.cursor() as cursor:
            self.active_jobs_cache = load_active_jobs_cache(cursor, company_id)
        
    def check_existing_job(self, job_url: str) -> Optional[int]:
        """Check if job URL already exists using cache first, then database"""
        job_id = check_job_in_cache(job_url, self.active_jobs_cache)
        if job_id:
            return job_id
        return None
    
    def update_job_verified_timestamp(self, job_id: int):
        """Update timestamp for a job that was verified to still exist"""
        with self.conn.cursor() as cursor:
            update_job_verified_timestamp(cursor, job_id)

    def store_job_listing(self, job_data: Dict, company_id: int) -> int:
        """Store new job listing using posting_operations"""
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM functions WHERE name = 'Other'")
            other_function = cursor.fetchone()
            enhanced_job_data = job_data.copy()
            enhanced_job_data.update({
                'job_type_id': None,
                'function': other_function['id'] if other_function else None,
                'office_location_id': 1,  # Default to In Office for government jobs
                'city_id': get_city_id(cursor, 'Jenks')
            })

            return store_job_listing(cursor, enhanced_job_data, company_id, 'City of Jenks')

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

class JenksJobScraper:
    """City of Jenks job scraper"""
    COMPANY_NAME = 'City of Jenks'
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        # Retrieve company config from database
        with self.db.conn.cursor() as cursor:
            self.company_config = get_company_config_by_name(cursor, self.COMPANY_NAME)
        if not self.company_config:
            raise ValueError(f"Company '{self.COMPANY_NAME}' not found in database")
        
        self.company_id = self.company_config['id']        
        self.logger = setup_logging(self.company_config['name'])
        self.db.logger = self.logger

        # Setup requests session
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
    def get_job_listings(self, job_board_url: str) -> List[Dict]:
        """Get all job listings from Jenks employment page"""
        try:
            self.logger.info(f"Loading job board: {job_board_url}")
            response = self.session.get(job_board_url)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Find job listing items only within the section with actual document IDs
            # Look for the relatedDocumentsSection that has data-documentids with actual values
            job_section = soup.find('div', class_='relatedDocumentsSection', attrs={'data-documentids': True})
            if job_section and job_section.get('data-documentids'):
                # Only get widgetItems from this specific section
                job_elements = job_section.find_all('li', class_='widgetItem')
            else:
                job_elements = []
            
            self.logger.info(f"Found {len(job_elements)} job listings")
            
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
            
        except Exception as e:
            self.logger.error(f"Error loading job board: {e}")
            return []
    
    def extract_job_metadata(self, job_element, job_number: int) -> Dict:
        """Extract job metadata from a single job element"""
        try:
            # Find the anchor tag within the job element
            link_element = job_element.find('a')
            if not link_element:
                self.logger.warning(f"Job {job_number}: No link found")
                return None
            
            # Extract job title (text content of the link)
            job_title = link_element.get_text(strip=True)
            # Remove the PDF suffix if present
            if job_title.endswith(' PDF'):
                job_title = job_title[:-4].strip()
            
            # Extract href and build full URL
            href = link_element.get('href')
            if not href:
                self.logger.warning(f"Job {job_number}: No href found")
                return None
            
            # Build full URL
            if href.startswith('http'):
                posting_url = href
            else:
                posting_url = f"https://www.jenks.com{href}"
            
            job_data = {
                'job_title': job_title,
                'posting_url': posting_url
            }
            
            self.logger.info(f"Job {job_number}: {job_title}")
            return job_data
            
        except Exception as e:
            self.logger.error(f"Error extracting metadata for job {job_number}: {e}")
            return None
    
    def download_and_extract_pdf(self, pdf_url: str) -> str:
        """Download PDF and extract text content"""
        try:
            self.logger.info(f"  Downloading PDF: {pdf_url}")
            response = self.session.get(pdf_url, timeout=30)
            response.raise_for_status()
            
            # Check if response is actually a PDF
            content_type = response.headers.get('content-type', '').lower()
            if 'pdf' not in content_type:
                self.logger.warning(f"  URL may not be a PDF (content-type: {content_type})")
            
            # Read PDF from memory
            pdf_file = io.BytesIO(response.content)
            pdf_reader = PyPDF2.PdfReader(pdf_file)
            
            # Extract text from all pages
            text_content = ""
            for page_num in range(len(pdf_reader.pages)):
                page = pdf_reader.pages[page_num]
                text_content += page.extract_text() + "\n"
            
            # Clean up the text
            text_content = text_content.strip()
            
            if len(text_content) > 100:
                self.logger.info(f"  Extracted {len(text_content)} characters from PDF")
                return text_content
            else:
                self.logger.warning(f"  PDF extraction yielded minimal text: {len(text_content)} characters")
                return text_content
                
        except Exception as e:
            self.logger.error(f"  Error downloading/extracting PDF: {e}")
            return ""
    
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
            self.logger.info("Step 2: Getting job listings from Jenks job board...")
            job_listings = self.get_job_listings(self.company_config['jobboard'])
            if not job_listings:
                raise Exception("No jobs retrieved from job board")
            
            stats['found'] = len(job_listings)
            self.logger.info(f"? Found {len(job_listings)} jobs")
            
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
                    
                    # Download and extract PDF content for new jobs
                    job_description = self.download_and_extract_pdf(job_metadata['posting_url'])
                    if not job_description or len(job_description.strip()) < 50:
                        self.logger.warning(f"  Failed to extract meaningful content from PDF")
                        stats['skipped'] += 1
                        continue
                    
                    # Prepare job data
                    job_data = {
                        'job_title': job_metadata['job_title'],
                        'posting_url': job_metadata['posting_url'],
                        'posting_id': None,  # Not available from Jenks
                        'job_description': job_description,
                        'date_posted': datetime.now().date(),  # Use current date since not available
                        'schedule': None,  # Not available from listing page
                        'job_category': None,  # Not available from listing page
                        'location_type': None,  # Not available from listing page
                        'minimum_salary': None,
                        'maximum_salary': None,
                        'pay_frequency': None,
                        'scraping_hash': None
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
            self.db.log_scraping_activity('City of Jenks', stats)
            
        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            self.logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats

def main():
    """Main execution function"""
    scraper = None
    try:
        # Initialize components
        db_manager = DatabaseManager()
        scraper = JenksJobScraper(db_manager)
        
        # Run scraping
        scraper.logger.info("Starting City of Jenks job scraping...")
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
        pass  # No selenium cleanup needed
    
    return 0

if __name__ == "__main__":
    exit(main())
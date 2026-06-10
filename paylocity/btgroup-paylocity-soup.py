#!/usr/bin/env python3
"""
Paylocity Job Board Scraper for B+T Group
Scrapes job listings using requests/BeautifulSoup (no AI extraction)
"""

from utils.date_utilities import normalize_date_string
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import hashlib
import logging
from typing import Dict, List, Optional
import time
import os
import sys

# Import the shared database manager
from database_manager import DatabaseManager
import psycopg
from psycopg.rows import dict_row

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('paylocity_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class PaylocityScraper:
    """Scraper for Paylocity job boards using requests/BeautifulSoup"""
    
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager
        self.session = requests.Session()
        
        # Company configuration - hardcoded for B+T Group
        self.company_config = {
            'name': 'B+T Group',
            'website': 'https://btgrp.com/',
            'jobboard_url': 'https://recruiting.paylocity.com/Recruiting/Jobs/All/cd12c8d2-57d4-40ff-85d8-c6cf36b2e7da/BT-Group?location=Tulsa%2C%20OK&department=All%20Departments',
            'base_detail_url': 'https://recruiting.paylocity.com/Recruiting/Jobs/Details/'
        }
        
        # Set up session headers
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })
    
    def get_job_type_id(self, job_type_name: str) -> Optional[int]:
        """Get job type ID from JobType lookup table"""
        try:
            with self.db.conn.cursor() as cursor:
                cursor.execute("SELECT id FROM JobType WHERE name = %s", (job_type_name,))
                result = cursor.fetchone()
                if result:
                    return result['id']
                else:
                    logger.warning(f"Job type '{job_type_name}' not found in JobType table")
                    return None
        except Exception as e:
            logger.error(f"Error looking up job type '{job_type_name}': {e}")
            return None
            
    def get_job_listings(self) -> List[Dict]:
        """Get job listings from the main Paylocity page"""
        try:
            logger.info("Fetching job listings from Paylocity...")
            response = self.session.get(self.company_config['jobboard_url'])
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            jobs = []
            
            # Look for job listing containers
            # Paylocity typically uses div elements with job information
            job_containers = soup.find_all('div', class_=lambda x: x and ('job' in x.lower() or 'position' in x.lower()))
            
            if not job_containers:
                # Fallback: look for any divs that might contain job listings
                job_containers = soup.find_all('div', attrs={'data-job-id': True})
            
            if not job_containers:
                # Another fallback: look for links to job details
                job_links = soup.find_all('a', href=re.compile(r'/Jobs/Details/\d+'))
                for link in job_links:
                    # Extract job ID from URL
                    job_id_match = re.search(r'/Jobs/Details/(\d+)', link['href'])
                    if job_id_match:
                        job_id = job_id_match.group(1)
                        
                        # Get job title from link text or nearby elements
                        job_title = link.get_text(strip=True)
                        if not job_title:
                            # Look for title in parent elements
                            parent = link.parent
                            if parent:
                                job_title = parent.get_text(strip=True)
                        
                        # Look for date in nearby elements
                        date_posted = None
                        for sibling in link.parent.find_all(text=re.compile(r'\d{1,2}/\d{1,2}/\d{4}')):
                            date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', sibling)
                            if date_match:
                                date_posted = date_match.group(1)
                                break
                        
                        jobs.append({
                            'title': job_title,
                            'job_id': job_id,
                            'detail_url': f"{self.company_config['base_detail_url']}{job_id}",
                            'date_posted': date_posted
                        })
            else:
                # Process job containers
                for container in job_containers:
                    try:
                        # Extract job title
                        title_element = container.find(['h2', 'h3', 'h4', 'a'])
                        job_title = title_element.get_text(strip=True) if title_element else "Unknown Title"
                        
                        # Extract job ID from links
                        job_link = container.find('a', href=re.compile(r'/Jobs/Details/\d+'))
                        if not job_link:
                            continue
                        
                        job_id_match = re.search(r'/Jobs/Details/(\d+)', job_link['href'])
                        if not job_id_match:
                            continue
                        
                        job_id = job_id_match.group(1)
                        
                        # Extract date posted
                        date_text = container.get_text()
                        date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', date_text)
                        date_posted = date_match.group(1) if date_match else None
                        
                        jobs.append({
                            'title': job_title,
                            'job_id': job_id,
                            'detail_url': f"{self.company_config['base_detail_url']}{job_id}",
                            'date_posted': date_posted
                        })
                        
                    except Exception as e:
                        logger.warning(f"Error parsing job container: {e}")
                        continue
            
            logger.info(f"Found {len(jobs)} job listings")
            return jobs
            
        except Exception as e:
            logger.error(f"Error fetching job listings: {e}")
            return []
    
    def get_job_details(self, detail_url: str) -> Dict:
        """Get job description and type from detail page"""
        try:
            logger.info(f"  Fetching job details from: {detail_url}")
            response = self.session.get(detail_url)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract job description
            # Look for common job description containers
            description_selectors = [
                '[class*="job-description"]',
                '[class*="jobDescription"]',
                '[class*="description"]',
                '[id*="description"]',
                '.content',
                '.job-content',
                'main',
                '[role="main"]'
            ]
            
            job_description = ""
            for selector in description_selectors:
                desc_element = soup.select_one(selector)
                if desc_element and len(desc_element.get_text(strip=True)) > 100:
                    job_description = str(desc_element)
                    logger.info(f"    Found description using selector: {selector}")
                    break
            
            if not job_description:
                # Fallback: get body content, removing navigation/header/footer
                body = soup.find('body')
                if body:
                    # Remove common non-content elements
                    for tag in body.find_all(['nav', 'header', 'footer', 'script', 'style', 'aside']):
                        tag.decompose()
                    job_description = str(body)
                    logger.info(f"    Using body content as fallback")
            
            # Extract job type (Full-time/Part-time)
            page_text = soup.get_text().lower()
            job_type = None
            
            if 'full-time' in page_text or 'full time' in page_text:
                job_type = 'Full-time'
            elif 'part-time' in page_text or 'part time' in page_text:
                job_type = 'Part-time'
            
            logger.info(f"    Extracted description: {len(job_description)} chars, Job type: {job_type}")
            
            return {
                'description': job_description,
                'job_type': job_type
            }
            
        except Exception as e:
            logger.error(f"  Error fetching job details: {e}")
            return {
                'description': "",
                'job_type': None
            }
    
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
            # Step 1: Get/create company
            logger.info("Step 1: Getting/creating company...")
            company_id = self.db.get_or_create_company(self.company_config)
            logger.info(f"? Company ID: {company_id}")
            
            # Step 2: Get job listings
            logger.info("Step 2: Getting job listings...")
            job_listings = self.get_job_listings()
            if not job_listings:
                raise Exception("No job listings found")
            
            stats['found'] = len(job_listings)
            logger.info(f"? Found {len(job_listings)} job listings")
            
            # Step 3: Process each job
            for i, job_listing in enumerate(job_listings):
                try:
                    logger.info(f"Processing job {i+1}/{len(job_listings)}: {job_listing['title']}")
                    
                    # Get job details
                    job_details = self.get_job_details(job_listing['detail_url'])
                    
                    # Skip if no description found
                    if not job_details['description'] or len(job_details['description'].strip()) < 50:
                        logger.warning(f"  Insufficient job description content")
                        stats['skipped'] += 1
                        continue
                    
                    # Get job type ID from JobType table
                    job_type_id = None
                    if job_details['job_type']:
                        job_type_id = self.get_job_type_id(job_details['job_type'])
                    
                    # Prepare job data with extracted fields for job type
                    extracted_fields = {
                        'job_type_id': job_type_id
                    } if job_type_id else None
                    
                    # Prepare job data
                    job_data = {
                        'title': job_listing['title'],
                        'url': job_listing['detail_url'],
                        'description': job_details['description'],
                        'date_posted': normalize_date_string(job_listing['date_posted']),
                        'scraping_hash': self.create_scraping_hash({
                            'title': job_listing['title'],
                            'url': job_listing['detail_url'],
                            'description': job_details['description']
                        })
                    }
                    
                    # Store job in database using existing method
                    job_id = self.db.store_job_listing(job_data, company_id, extracted_fields)
                    logger.info(f"  ? Stored job with ID: {job_id}")
                    
                    stats['added'] += 1
                    
                    # Be respectful with timing
                    time.sleep(1)
                    
                except Exception as e:
                    error_msg = f"Error processing job {job_listing.get('title', 'Unknown')}: {e}"
                    logger.error(error_msg)
                    stats['errors'].append(error_msg)
                    stats['skipped'] += 1
            
            # Step 4: Log results
            logger.info("Step 4: Logging results...")
            self.db.log_scraping_activity('Paylocity B+T Group', stats)
            
        except Exception as e:
            error_msg = f"Scraping failed: {e}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)
        
        return stats
    
    def store_job_listing_with_type(self, job_data: Dict, company_id: int, job_type_id: Optional[int]) -> int:
        """Store job listing with job type - modified version of DatabaseManager.store_job_listing"""
        with self.db.conn.cursor() as cursor:
            # Check for existing job by URL and title+company
            cursor.execute("""
                SELECT id FROM JobListings 
                WHERE posting_url = %s 
                OR (job_title = %s AND companyid = %s)
            """, (job_data['url'], job_data['title'], company_id))
            
            existing = cursor.fetchone()
            
            # Try to map job title to function
            function_id = self.db._map_job_to_function(job_data['title'])
            
            if existing:
                # Update existing job
                cursor.execute("""
                    UPDATE JobListings SET
                        job_title = %s,
                        job_description = %s,
                        posting_url = %s,
                        date_posted = %s,
                        scraping_hash = %s,
                        function_id = %s,
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
                    function_id,
                    job_type_id,
                    existing['id']
                ))
                result = cursor.fetchone()
                logger.info(f"  Updated existing job: {job_data['title']} (ID: {existing['id']})")
                return result['id']
            else:
                # Insert new job
                cursor.execute("""
                    INSERT INTO JobListings (
                        companyid, job_title, job_description, posting_url, 
                        source_job_board, date_posted, scraping_hash, 
                        function_id, job_type_id, approved, job_status_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             (SELECT id FROM JobStatus WHERE name = 'Active'))
                    RETURNING id
                """, (
                    company_id,
                    job_data['title'],
                    job_data['description'],
                    job_data['url'],
                    'Paylocity',
                    job_data['date_posted'],
                    job_data['scraping_hash'],
                    function_id,
                    job_type_id,
                    True
                ))
                
                result = cursor.fetchone()
                job_id = result['id']
                logger.info(f"  Created new job: {job_data['title']} (ID: {job_id})")
                return job_id

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
        scraper = PaylocityScraper(db_manager)
        
        # Run scraping
        logger.info("Starting Paylocity B+T Group job scraping...")
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
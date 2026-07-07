#!/usr/bin/env python3
"""
paragon-films-adp-api-selenium.py
Paragon Films ADP Job Board Scraper
Combines API calls with targeted Selenium scraping for job descriptions
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
import hashlib
import re
from datetime import datetime
from bs4 import BeautifulSoup
import logging
from typing import Dict, List, Optional
import requests

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import check_existing_job_by_url, store_job_listing, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.utility_methods import normalize_job_type
from utils.selenium_config import SeleniumConfig
from utils.location_utilities import find_served_city, get_city_id

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('paragon_scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Manufacturing-focused function keyword mapping
_FUNCTION_KEYWORDS = {
    'Manufacturing': [
        'manufacturing', 'production', 'assembly', 'fabrication', 'machining',
        'operator', 'assembler', 'fabricator', 'line', 'plant', 'factory'
    ],
    'Machinist': [
        'machinist', 'cnc', 'lathe', 'mill', 'grinder', 'machine operator'
    ],
    'Skilled Trades': [
        'welder', 'electrician', 'mechanic', 'technician', 'maintenance',
        'repair', 'installer', 'fitter', 'pipefitter'
    ],
    'Quality': [
        'quality', 'qa', 'qc', 'inspector', 'assurance', 'control'
    ],
    'Engineering': [
        'mechanical', 'mech eng', 'mechanical engineer', 'electrical', 'elec eng',
        'electrical engineer', 'civil', 'civil engineer', 'process engineer',
        'industrial engineer', 'design engineer',
    ],
    'Information Technology': [
        'software', 'developer', 'programmer', 'engineer', 'data',
        'database', 'system', 'network', 'security', 'devops', 'cloud',
        'application', 'web', 'mobile', 'qa', 'scrum', 'agile'
    ],
    'Accounting': [
        'finance', 'financial', 'accounting', 'accountant', 'treasury', 'controller',
        'audit', 'bookkeeping', 'clerk', 'accounting clerk'
    ],
    'Customer Support': [
        'customer service', 'support', 'help desk', 'call center', 'client',
        'representative', 'relationship'
    ],
    'Sales': [
        'sales', 'account manager', 'business development', 'bd', 'revenue'
    ],
    'Human Resources': ['hr', 'human resources', 'recruiter', 'talent', 'people', 'benefits'],
    'Marketing': ['marketing', 'brand', 'digital marketing', 'content', 'social media', 'communications'],
    'Legal': ['legal', 'attorney', 'lawyer', 'counsel', 'compliance', 'contract'],
    'Operations': [
        'operations', 'ops', 'supply chain', 'process', 'facility',
        'transportation', 'logistics', 'shipping', 'warehouse', 'forklift',
        'driver', 'delivery', 'material handler', 'inventory',
        'project manager', 'program manager', 'scrum master', 'project coordinator',
    ],
    'Administrative': ['admin', 'administrative', 'coordinator', 'assistant', 'office'],
    'Security': ['security', 'safety', 'guard', 'protection'],
    'Purchasing': ['purchasing', 'buyer', 'procurement', 'sourcing'],
    'Science': ['research', 'development', 'r&d', 'scientist']
}


def _map_job_to_function(cursor, job_title: str) -> Optional[int]:
    """Map job title to function ID using manufacturing-specific keywords"""
    title_lower = job_title.lower()
    for function_name, keywords in _FUNCTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in title_lower:
                cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
                result = cursor.fetchone()
                if result:
                    logger.info(f"  Mapped '{job_title}' to function: {function_name}")
                    return result['id']
    cursor.execute("SELECT id FROM functions WHERE name = %s", ('Other',))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{job_title}' to function: Other (no specific match)")
        return result['id']
    logger.warning(f"  Could not map '{job_title}' to any function")
    return None


def _map_job_type(cursor, work_level_code: str) -> Optional[int]:
    """Map ADP work level code to job_type_id via normalize_job_type"""
    canonical = normalize_job_type(work_level_code)
    if not canonical:
        logger.warning(f"  Could not map '{work_level_code}' to any job type")
        return None
    cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
    result = cursor.fetchone()
    if result:
        logger.info(f"  Mapped '{work_level_code}' to job type: {canonical}")
        return result['id']
    logger.warning(f"  Job type '{canonical}' not found in database")
    return None


def _update_company_scrape_completed(cursor, company_id: int):
    """Update last_full_scrape_completed timestamp for company"""
    cursor.execute("""
        UPDATE company
        SET last_full_scrape_completed = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (company_id,))
    logger.info(f"Updated last_full_scrape_completed for company {company_id}")


def _log_scraping_activity(cursor, job_board: str, company_id: int, stats: Dict):
    """Log scraping results to scrapinglog table"""
    cursor.execute("""
        INSERT INTO scrapinglog (
            job_board, company_id, jobs_found, jobs_added, jobs_updated,
            jobs_skipped, errors, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        job_board,
        company_id,
        stats.get('found', 0),
        stats.get('added', 0),
        stats.get('updated', 0),
        stats.get('skipped', 0),
        str(stats.get('errors', [])),
        'completed'
    ))


class SeleniumJobScraper:
    """Handles JavaScript-heavy job pages using Selenium"""

    def __init__(self, headless=True):
        self.driver = None
        self.headless = headless
        self.setup_driver()

    def setup_driver(self):
        """Initialize Chrome WebDriver"""
        try:
            chrome_options = SeleniumConfig.get_chrome_options(headless=self.headless)
            self.driver = webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=chrome_options
            )
            SeleniumConfig.setup_driver_timeouts(self.driver)
            logger.info("Selenium WebDriver initialized")
        except Exception as e:
            logger.error(f"Failed to initialize WebDriver: {e}")
            raise

    def get_job_content(self, job_url: str, timeout=12) -> str:
        """Load job page and wait for content to render"""
        try:
            logger.info(f"  Loading job page with Selenium...")
            self.driver.get(job_url)

            wait = WebDriverWait(self.driver, timeout)
            try:
                wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            except TimeoutException:
                logger.warning("  Body tag not found within timeout")
                return ""

            time.sleep(1.5)

            page_source = self.driver.page_source
            logger.info(f"  Retrieved page source: {len(page_source)} characters")
            return page_source

        except TimeoutException:
            logger.warning("  Timeout waiting for page to load")
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
            except Exception:
                pass


class ParagonFilmsJobScraper:
    """Paragon Films ADP job scraper combining API calls with Selenium"""

    def __init__(self, conn):
        self.conn = conn
        self.selenium_scraper = SeleniumJobScraper(headless=True)
        self.session = requests.Session()

        self.company_config = {
            'name': 'Paragon Films',
            'website': 'https://www.paragonfilms.com/',
            'jobboard_url': 'https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=35bfe306-1df9-4834-aac4-18f66e86a043&ccId=9200673723583_2&lang=en_US',
            'api_endpoint': 'https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions',
            'cid': '35bfe306-1df9-4834-aac4-18f66e86a043',
            'ccId': '9200673723583_2'
        }

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept': 'application/json',
            'DNT': '1',
            'Sec-GPC': '1'
        })

    def get_job_listings_from_api(self) -> List[Dict]:
        """Get all job listings from Paragon Films ADP API"""
        try:
            logger.info("Fetching job listings from ADP API...")

            timestamp = int(time.time() * 1000)
            params = {
                'cid': self.company_config['cid'],
                'timeStamp': timestamp,
                'ccId': self.company_config['ccId'],
                'lang': 'en_US',
                'locale': 'en_US',
                '$top': 100
            }

            response = self.session.get(
                self.company_config['api_endpoint'],
                params=params,
                headers={'Referer': self.company_config['jobboard_url']}
            )
            response.raise_for_status()
            data = response.json()

            if 'jobRequisitions' not in data:
                logger.warning("No jobRequisitions found in API response")
                return []

            jobs = data['jobRequisitions']
            logger.info(f"Retrieved {len(jobs)} jobs from ADP API")
            return jobs

        except Exception as e:
            logger.error(f"Error fetching jobs from API: {e}")
            return []

    def filter_served_city_jobs(self, jobs: List[Dict]) -> List[Dict]:
        """Filter jobs to those located in a served city"""
        filtered = []
        logger.info(f"Filtering {len(jobs)} jobs for served cities...")

        for job in jobs:
            locations = job.get('requisitionLocations', [])
            for location in locations:
                short_name = location.get('nameCode', {}).get('shortName', '').strip()
                city_name = location.get('address', {}).get('cityName', '').strip()

                matched = find_served_city(city_name) or find_served_city(short_name)
                if matched:
                    filtered.append(job)
                    logger.info(f"  ✓ {matched}: {job.get('requisitionTitle', 'Unknown')}")
                    break

        logger.info(f"Found {len(filtered)} jobs in served cities")
        return filtered

    def clean_job_title(self, title: str):
        """
        Clean Paragon-specific title patterns and extract metadata.
        Returns (cleaned_title, experience_id, first_shift, third_shift).
        """
        cleaned = title
        experience_id = None
        first_shift = False
        third_shift = False

        if 'Entry Level Position' in cleaned:
            cleaned = cleaned.replace('Entry Level Position', '').strip()
            experience_id = 1
            logger.info("  Detected entry level position, set experience_id = 1")

        cleaned = re.sub(r'\s*-\s*OK\s*-\s*', '', cleaned).strip()

        if 'DAYS' in title.upper():
            first_shift = True
            cleaned = re.sub(r'\bDAYS\b', '', cleaned, flags=re.IGNORECASE).strip()
            logger.info("  Detected DAYS in title, set first_shift = True")

        if 'NIGHTS' in title.upper():
            third_shift = True
            cleaned = re.sub(r'\bNIGHTS\b', '', cleaned, flags=re.IGNORECASE).strip()
            logger.info("  Detected NIGHTS in title, set third_shift = True")

        cleaned = ' '.join(cleaned.split())

        if cleaned != title:
            logger.info(f"  Title cleaned: '{title}' → '{cleaned}'")

        return cleaned, experience_id, first_shift, third_shift

    def extract_api_job_data(self, job: Dict) -> Dict:
        """Extract structured data from API job response"""
        try:
            original_title = job.get('requisitionTitle', '')
            cleaned_title, experience_id, first_shift, third_shift = self.clean_job_title(original_title)

            job_data = {
                'title': cleaned_title,
                'external_job_id': None,
                'date_posted': None,
                'minimum_salary': None,
                'maximum_salary': None,
                'pay_frequency': None,
                'job_type': job.get('workLevelCode', {}).get('shortName', ''),
                'experience_id': experience_id,
                'first_shift': first_shift,
                'third_shift': third_shift,
                'city': None,
            }

            string_fields = job.get('customFieldGroup', {}).get('stringFields', [])
            for field in string_fields:
                if field.get('nameCode', {}).get('codeValue') == 'ExternalJobID':
                    job_data['external_job_id'] = field.get('stringValue')
                    break

            post_date = job.get('postDate')
            if post_date:
                try:
                    job_data['date_posted'] = datetime.fromisoformat(post_date.replace('Z', '+00:00'))
                except Exception:
                    logger.warning(f"Could not parse date: {post_date}")

            pay_grade_range = job.get('payGradeRange', {})
            if pay_grade_range:
                min_rate = pay_grade_range.get('minimumRate', {})
                max_rate = pay_grade_range.get('maximumRate', {})
                if min_rate and 'amountValue' in min_rate:
                    job_data['minimum_salary'] = min_rate['amountValue']
                if max_rate and 'amountValue' in max_rate:
                    job_data['maximum_salary'] = max_rate['amountValue']

            for location in job.get('requisitionLocations', []):
                city_name = location.get('address', {}).get('cityName', '').strip()
                short_name = location.get('nameCode', {}).get('shortName', '').strip()
                matched = find_served_city(city_name) or find_served_city(short_name)
                if matched:
                    job_data['city'] = matched
                    break

            return job_data

        except Exception as e:
            logger.error(f"Error extracting job data: {e}")
            return {}

    def build_job_url(self, external_job_id: str) -> str:
        """Build job detail URL using external job ID"""
        base_url = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
        params = {
            'cid': self.company_config['cid'],
            'ccId': self.company_config['ccId'],
            'type': 'MP',
            'lang': 'en_US',
            'selectedMenuKey': 'CareerCenter',
            'jobId': external_job_id
        }
        param_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return f"{base_url}?{param_string}"

    def scrape_job_description(self, external_job_id: str) -> str:
        """Scrape job description from detail page"""
        job_url = self.build_job_url(external_job_id)
        html_content = self.selenium_scraper.get_job_content(job_url)

        if not html_content:
            return ""

        try:
            soup = BeautifulSoup(html_content, 'html.parser')

            body = soup.find('body')
            if not body:
                logger.warning("No body tag found in job page")
                return ""

            for tag in body.find_all(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                tag.decompose()

            for br in body.find_all('br'):
                br.replace_with('\n')

            description = body.get_text(separator='\n', strip=True)

            copyright_idx = description.lower().find('copyright')
            if copyright_idx != -1:
                description = description[:copyright_idx]

            description = re.sub(r'\n{3,}', '\n\n', description).strip()

            logger.info(f"  Extracted job description: {len(description)} characters")
            return description

        except Exception as e:
            logger.warning(f"Error extracting job description: {e}")
            return html_content

    def create_scraping_hash(self, title: str, url: str, description: str) -> str:
        """Create hash for duplicate detection"""
        content = f"{title}{url}{description}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()

    def scrape_jobs(self) -> Dict:
        """Main scraping method"""
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        try:
            with self.conn.cursor() as cursor:
                # Step 1: Resolve company ID
                logger.info("Step 1: Resolving company ID...")
                company_id = get_or_create_company(cursor, {
                    'name': self.company_config['name'],
                    'website': self.company_config['website'],
                    'jobboard': self.company_config['jobboard_url'],
                    'company_type_name': 'Private Company'
                })
                logger.info(f"  Resolved company ID: {company_id}")

                # Step 2: Get job listings from API
                logger.info("Step 2: Getting job listings from API...")
                all_jobs = self.get_job_listings_from_api()
                if not all_jobs:
                    raise Exception("No jobs retrieved from API")

                # Step 3: Filter for served-city jobs
                logger.info("Step 3: Filtering for served cities...")
                local_jobs = self.filter_served_city_jobs(all_jobs)
                stats['found'] = len(local_jobs)

                if not local_jobs:
                    logger.warning("No jobs found in served cities")
                    return stats

                # Step 4: Process each job
                for i, job in enumerate(local_jobs):
                    try:
                        logger.info(f"Processing job {i+1}/{len(local_jobs)}: {job.get('requisitionTitle', 'Unknown')}")

                        api_data = self.extract_api_job_data(job)
                        if not api_data.get('external_job_id'):
                            logger.warning("  No external job ID found, skipping")
                            stats['skipped'] += 1
                            continue

                        job_url = self.build_job_url(api_data['external_job_id'])

                        existing_job_id = check_existing_job_by_url(cursor, job_url)
                        if existing_job_id:
                            stats['updated'] += 1
                            continue

                        job_description = self.scrape_job_description(api_data['external_job_id'])
                        if not job_description or len(job_description.strip()) < 50:
                            logger.warning("  Failed to get meaningful job description")
                            stats['skipped'] += 1
                            continue

                        city_id = get_city_id(cursor, api_data['city']) if api_data.get('city') else None

                        job_data = {
                            'job_title': api_data['title'],
                            'job_description': job_description,
                            'posting_url': job_url,
                            'source_job_board': 'Paragon Films ADP',
                            'date_posted': api_data['date_posted'],
                            'minimum_salary': api_data['minimum_salary'],
                            'maximum_salary': api_data['maximum_salary'],
                            'pay_frequency': api_data['pay_frequency'],
                            'external_job_id': api_data['external_job_id'],
                            'scraping_hash': self.create_scraping_hash(
                                api_data['title'], job_url, job_description
                            ),
                            'function': _map_job_to_function(cursor, api_data['title']),
                            'job_type_id': _map_job_type(cursor, api_data.get('job_type', '')),
                            'experience_id': api_data['experience_id'],
                            'first_shift': api_data['first_shift'],
                            'third_shift': api_data['third_shift'],
                            'city_id': city_id,
                        }

                        job_id = store_job_listing(cursor, job_data, company_id)
                        logger.info(f"  ✓ Stored job with ID: {job_id}")
                        stats['added'] += 1

                        time.sleep(1.0)

                    except Exception as e:
                        error_msg = f"Error processing job {job.get('requisitionTitle', 'Unknown')}: {e}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                        stats['skipped'] += 1

                # Step 5: Mark stale jobs as closed
                logger.info("Step 5: Marking stale jobs as closed...")
                mark_stale_jobs_closed(cursor, company_id)

                # Step 6: Update company scrape completion
                logger.info("Step 6: Updating company scrape completion...")
                _update_company_scrape_completed(cursor, company_id)

                # Step 7: Log results
                logger.info("Step 7: Logging results...")
                _log_scraping_activity(cursor, 'Paragon Films ADP', company_id, stats)

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
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = ParagonFilmsJobScraper(conn)

        logger.info("Starting Paragon Films ADP job scraping...")
        results = scraper.scrape_jobs()

        logger.info("=== SCRAPING SUMMARY ===")
        logger.info(f"Jobs found: {results['found']}")
        logger.info(f"Jobs added: {results['added']}")
        logger.info(f"Jobs updated: {results['updated']}")
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
        close_connection(conn)

    return 0


if __name__ == "__main__":
    exit(main())

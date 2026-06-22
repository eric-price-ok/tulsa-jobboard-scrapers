#!/usr/bin/env python3
"""
sodexo-icims-scrape.py
Sodexo iCIMS job board scraper (Gen 2)

Uses Selenium to load the iCIMS listing page (in_iframe=1 returns server-rendered
HTML with .iCIMS_JobCardItem elements). Detail pages are fetched the same way.

Listing URL is hardcoded to the Oklahoma-filtered search:
  https://external-careers-sodexo.icims.com/jobs/search?ss=1&searchRelation=keyword_all&searchLocation=-12820-&in_iframe=1

Key selectors used:
  Listing: div.iCIMS_JobCardItem
    - Title/URL: a.iCIMS_Anchor following "Position Posting Title" label
    - Location:  span following "Job Locations" label (format: US-OK-Tulsa)
    - Category:  dt.iCIMS_JobHeaderField[text=Category] + dd.iCIMS_JobHeaderData
  Detail:  div.iCIMS_JobHeaderTag dt+dd pairs, keyed by dt text
    - Employment Status  -> job type
    - (blank/&nbsp; dt) -> work location (On-Site / Remote / Hybrid)
    - Posted Range       -> salary (e.g. "$100130 to $129580")
    - div.iCIMS_JobContainer -> job description (keep p/br/h2/ul/ol/li/b/strong/em/i)
"""

from utils.db_connection import get_database_connection, close_connection
from utils.posting_operations import store_job_listing, check_existing_job_by_url, mark_stale_jobs_closed
from utils.company_operations import get_or_create_company
from utils.location_utilities import TULSA_METRO_CITIES
from utils.selenium_config import SeleniumConfig
from utils.utility_methods import setup_logging, normalize_job_type, normalize_work_location

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from bs4 import BeautifulSoup, NavigableString, Tag
import hashlib
import re
import time
from typing import Dict, List, Optional, Tuple

logger = setup_logging('Sodexo')

LISTING_URL = (
    'https://external-careers-sodexo.icims.com/jobs/search'
    '?ss=1&searchRelation=keyword_all&searchLocation=-12820-&in_iframe=1'
)

# Set to None for production
MAX_JOBS_ADDED = 5

_DESCRIPTION_KEEP_TAGS = {
    'p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'strong', 'b', 'em', 'i',
}


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------

def _parse_icims_salary(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse '$100130 to $129580' format from iCIMS Posted Range."""
    match = re.search(r'\$?([\d,]+)\s+to\s+\$?([\d,]+)', text, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1).replace(',', '')), float(match.group(2).replace(',', ''))
        except ValueError:
            pass
    return None, None


# ---------------------------------------------------------------------------
# Category → function mapping
# ---------------------------------------------------------------------------

_CATEGORY_MAP = {
    'facilities':        'Skilled Labor',
    'food':              'Skilled Labor',
    'culinary':          'Skilled Labor',
    'dining':            'Skilled Labor',
    'nutrition':         'Skilled Labor',
    'cleaning':          'Skilled Labor',
    'custodial':         'Skilled Labor',
    'maintenance':       'Skilled Labor',
    'technician':        'Skilled Labor',
    'housekeeping':      'Skilled Labor',
    'logistics':         'Skilled Labor',
    'transportation':    'Skilled Labor',
    'security':          'Security',
    'technology':        'Information Technology',
    'information tech':  'Information Technology',
    'it ':               'Information Technology',
    'software':          'Information Technology',
    'data':              'Information Technology',
    'finance':           'Finance',
    'accounting':        'Finance',
    'payroll':           'Finance',
    'human resource':    'Human Resources',
    'talent':            'Human Resources',
    'recruiting':        'Human Resources',
    'marketing':         'Marketing',
    'sales':             'Sales',
    'operations':        'Management',
    'management':        'Management',
    'director':          'Management',
    'admin':             'Administration',
    'project':           'Project Management',
    'customer':          'Customer Service',
    'clinical':          'Healthcare',
    'health':            'Healthcare',
    'nurse':             'Healthcare',
    'medical':           'Healthcare',
}


def _map_category_to_function(cursor, category: Optional[str], title: str) -> Optional[int]:
    search_text = (category or title or '').lower()
    for keyword, function_name in _CATEGORY_MAP.items():
        if keyword in search_text:
            cursor.execute("SELECT id FROM functions WHERE name = %s", (function_name,))
            row = cursor.fetchone()
            if row:
                logger.info(f"  Function: '{function_name}' (matched '{keyword}' in '{search_text[:40]}')")
                return row['id']
    cursor.execute("SELECT id FROM functions WHERE name = 'Other'")
    row = cursor.fetchone()
    logger.info(f"  Function: 'Other' (no match for '{search_text[:40]}')")
    return row['id'] if row else None


# ---------------------------------------------------------------------------
# HTML description cleaning
# ---------------------------------------------------------------------------

def _clean_description(container: Tag) -> str:
    for tag in container.find_all(['script', 'style', 'noscript']):
        tag.decompose()

    def serialize(node):
        if isinstance(node, NavigableString):
            return str(node)
        if isinstance(node, Tag):
            children = ''.join(serialize(child) for child in node.children)
            if node.name in _DESCRIPTION_KEEP_TAGS:
                return f'<{node.name}>{children}</{node.name}>'
            return children
        return ''

    html = serialize(container)
    html = re.sub(r'[ \t]+', ' ', html)
    html = re.sub(r'\n{3,}', '\n\n', html)
    return html.strip()


# ---------------------------------------------------------------------------
# Listing page parsing
# ---------------------------------------------------------------------------

def _is_tulsa_location(location_text: str) -> bool:
    if not location_text:
        return False
    lower = location_text.lower()
    return any(city.lower() in lower for city in TULSA_METRO_CITIES)


def _parse_card(card: Tag) -> Optional[Dict]:
    """
    Extract title, URL, location, and category from one iCIMS_JobCardItem.

    iCIMS uses a label-then-value pattern:
      - A span/dt with the field name, followed by the value element.
      - Title URL lives in a.iCIMS_Anchor next to the "Position Posting Title" label.
    """
    # --- Title & URL ---
    title_link = None
    for elem in card.find_all(string=re.compile(r'Position Posting Title', re.IGNORECASE)):
        parent = elem.find_parent()
        if parent:
            anchor = parent.find_next('a', class_='iCIMS_Anchor')
            if anchor:
                title_link = anchor
                break
    if not title_link:
        title_link = card.find('a', class_='iCIMS_Anchor')
    if not title_link:
        return None

    raw_title = title_link.get_text(strip=True)
    url = title_link.get('href', '')
    # title attribute is "id - Title Text"; prefer visible text if present
    title = raw_title or re.sub(r'^\d+\s*-\s*', '', title_link.get('title', ''))
    if not title or not url:
        return None

    # Ensure in_iframe=1 is present on the detail URL
    if 'in_iframe=1' not in url:
        sep = '&' if '?' in url else '?'
        url = url + sep + 'in_iframe=1'

    # --- Location ---
    location = None
    for elem in card.find_all(string=re.compile(r'Job Locations', re.IGNORECASE)):
        parent = elem.find_parent()
        if parent:
            nxt = parent.find_next_sibling()
            if nxt:
                location = nxt.get_text(strip=True)
                break

    # --- Category (dt.iCIMS_JobHeaderField + dd.iCIMS_JobHeaderData) ---
    category = None
    for dt in card.find_all('dt', class_='iCIMS_JobHeaderField'):
        if dt.get_text(strip=True).lower() == 'category':
            dd = dt.find_next_sibling('dd', class_='iCIMS_JobHeaderData')
            if dd:
                category = dd.get_text(strip=True)
            break

    logger.info(f"  Card: {title!r} | loc={location!r} | cat={category!r}")
    return {'job_title': title, 'posting_url': url, 'location': location, 'category': category}


def _get_next_page_url(soup: BeautifulSoup) -> Optional[str]:
    """Return the URL of the next page, or None if on the last page."""
    for selector in [
        'a[rel="next"]',
        'a.iCIMS_Pager_Next',
        'a[class*="pager-next"]',
        'a[class*="Next"]',
    ]:
        link = soup.select_one(selector)
        if link and link.get('href'):
            return link['href']
    # Look for a "Next" text link inside a pager
    for a in soup.find_all('a'):
        txt = a.get_text(strip=True)
        if txt in ('Next', 'Next >', '»', '>'):
            href = a.get('href', '')
            if href and href != '#':
                return href
    return None


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def _extract_header_tags(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Collect all div.iCIMS_JobHeaderTag dt/dd pairs into a dict.
    Key is the dt text (stripped, nbsp removed). Blank key holds the
    unlabeled field (work location in iCIMS).
    """
    tags = {}
    for div in soup.find_all('div', class_='iCIMS_JobHeaderTag'):
        dt = div.find('dt', class_='iCIMS_JobHeaderField')
        dd = div.find('dd', class_='iCIMS_JobHeaderData')
        if dt and dd:
            key = dt.get_text(strip=True).replace('\xa0', '').strip()
            val = dd.get_text(strip=True)
            if key not in tags:  # keep first occurrence if duplicated
                tags[key] = val
    return tags


def _parse_detail(cursor, html: str) -> Dict:
    soup = BeautifulSoup(html, 'html.parser')
    result: Dict = {}

    tags = _extract_header_tags(soup)
    logger.debug(f"  Header tags: {tags}")

    # Employment Status → job type
    emp_status = tags.get('Employment Status', '')
    if emp_status:
        canonical = normalize_job_type(emp_status)
        if canonical:
            cursor.execute("SELECT id FROM jobtype WHERE name = %s", (canonical,))
            row = cursor.fetchone()
            if row:
                result['job_type_id'] = row['id']
                logger.info(f"  Job type: '{emp_status}' -> '{canonical}'")

    # Blank/nbsp label → work location
    work_loc_raw = tags.get('', '')
    if work_loc_raw:
        canonical = normalize_work_location(work_loc_raw)
        if canonical:
            cursor.execute("SELECT id FROM officelocations WHERE name = %s", (canonical,))
            row = cursor.fetchone()
            if row:
                result['office_location_id'] = row['id']
                logger.info(f"  Work location: '{work_loc_raw}' -> '{canonical}'")

    # Posted Range → salary
    salary_raw = tags.get('Posted Range', '')
    if salary_raw:
        span_elem = None
        for div in soup.find_all('div', class_='iCIMS_JobHeaderTag'):
            dt = div.find('dt', class_='iCIMS_JobHeaderField')
            if dt and 'Posted Range' in dt.get_text():
                span_elem = div.find('span')
                break
        salary_text = span_elem.get_text(strip=True) if span_elem else salary_raw
        min_sal, max_sal = _parse_icims_salary(salary_text)
        if min_sal:
            result['minimum_salary'] = min_sal
            result['maximum_salary'] = max_sal
            logger.info(f"  Salary: {min_sal} – {max_sal}")

    # Description from div.iCIMS_JobContainer
    container = soup.find('div', class_='iCIMS_JobContainer')
    if container:
        result['job_description'] = _clean_description(container)
        logger.info(f"  Description: {len(result['job_description'])} chars")
    else:
        logger.warning("  iCIMS_JobContainer not found on detail page")
        result['job_description'] = ''

    return result


# ---------------------------------------------------------------------------
# Selenium helper
# ---------------------------------------------------------------------------

class _Driver:
    def __init__(self):
        options = SeleniumConfig.get_chrome_options(headless=True)
        self.driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        SeleniumConfig.setup_driver_timeouts(self.driver)
        logger.info("WebDriver initialized")

    def get_html(self, url: str, wait_selector: str = 'body', timeout: int = 20) -> str:
        self.driver.get(url)
        try:
            WebDriverWait(self.driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
            )
        except TimeoutException:
            logger.warning(f"Timeout waiting for {wait_selector!r} on {url}")
        time.sleep(1.5)
        return self.driver.page_source

    def quit(self):
        try:
            self.driver.quit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

class SodexoScraper:

    COMPANY_CONFIG = {
        'name':              'Sodexo',
        'website':           'https://www.sodexo.com',
        'company_type_name': 'Public Company',
        'source_job_board':  'Sodexo iCIMS',
    }

    def __init__(self, conn):
        self.conn = conn
        self.driver = _Driver()

    def _get_all_listings(self) -> List[Dict]:
        jobs: List[Dict] = []
        url = LISTING_URL
        page_num = 1

        while url:
            logger.info(f"Loading listing page {page_num}: {url}")
            html = self.driver.get_html(url, wait_selector='.iCIMS_JobCardItem, body')
            soup = BeautifulSoup(html, 'html.parser')
            logger.info(f"  Page title: {soup.title.string if soup.title else '(none)'}")

            cards = soup.find_all('div', class_='iCIMS_JobCardItem')
            if not cards:
                logger.warning("  No .iCIMS_JobCardItem elements found")
                logger.warning(f"  Body snippet: {html[500:1500]}")
                break

            logger.info(f"  Found {len(cards)} cards")
            for card in cards:
                meta = _parse_card(card)
                if meta:
                    jobs.append(meta)

            url = _get_next_page_url(soup)
            if url and 'in_iframe=1' not in url:
                sep = '&' if '?' in url else '?'
                url = url + sep + 'in_iframe=1'
            page_num += 1

        logger.info(f"Total listings scraped: {len(jobs)}")
        return jobs

    def scrape_jobs(self) -> Dict:
        stats = {'found': 0, 'added': 0, 'updated': 0, 'skipped': 0, 'errors': []}

        with self.conn.cursor() as cursor:
            # Resolve company
            company_id = get_or_create_company(cursor, self.COMPANY_CONFIG)
            logger.info(f"Company ID: {company_id}")

            # Look up Tulsa city_id
            cursor.execute("SELECT id FROM cities WHERE city_name = 'Tulsa'")
            row = cursor.fetchone()
            tulsa_city_id = row['id'] if row else None

            # Default On-site office location
            cursor.execute("SELECT id FROM officelocations WHERE name = 'On-site'")
            row = cursor.fetchone()
            default_office_id = row['id'] if row else None

            # Fetch and filter listings
            all_listings = self._get_all_listings()
            tulsa_listings = [j for j in all_listings if _is_tulsa_location(j.get('location', ''))]
            logger.info(f"After Tulsa filter: {len(tulsa_listings)}/{len(all_listings)}")
            stats['found'] = len(tulsa_listings)

            for i, meta in enumerate(tulsa_listings):
                title = meta['job_title']
                url   = meta['posting_url']
                logger.info(f"Job {i+1}/{len(tulsa_listings)}: {title}")

                try:
                    existing_id = check_existing_job_by_url(cursor, url)
                    if existing_id:
                        logger.info("  Already in DB, skipping")
                        stats['updated'] += 1
                        continue

                    # Load detail page
                    detail_html = self.driver.get_html(
                        url,
                        wait_selector='.iCIMS_JobContainer, .iCIMS_JobHeaderTag, body'
                    )
                    if len(detail_html.strip()) < 200:
                        logger.warning("  Detail page too short, skipping")
                        stats['skipped'] += 1
                        continue

                    detail = _parse_detail(cursor, detail_html)
                    description = detail.get('job_description', '')
                    if len(description.strip()) < 100:
                        logger.warning("  Description too short, skipping")
                        stats['skipped'] += 1
                        continue

                    job_data = {
                        'job_title':          title,
                        'job_description':    description,
                        'posting_url':        url,
                        'scraping_hash':      hashlib.md5(f"{title}{url}{description}".encode()).hexdigest(),
                        'function':           _map_category_to_function(cursor, meta.get('category'), title),
                        'job_type_id':        detail.get('job_type_id'),
                        'office_location_id': detail.get('office_location_id') or default_office_id,
                        'city_id':            tulsa_city_id,
                        'minimum_salary':     detail.get('minimum_salary'),
                        'maximum_salary':     detail.get('maximum_salary'),
                    }

                    job_id = store_job_listing(cursor, job_data, company_id, self.COMPANY_CONFIG['source_job_board'])
                    logger.info(f"  Stored -> job ID {job_id}")
                    stats['added'] += 1

                    if MAX_JOBS_ADDED and stats['added'] >= MAX_JOBS_ADDED:
                        logger.info(f"Reached MAX_JOBS_ADDED={MAX_JOBS_ADDED}, stopping early")
                        break

                    time.sleep(0.5)

                except Exception as e:
                    msg = f"Error on '{title}': {e}"
                    logger.error(msg)
                    stats['errors'].append(msg)
                    stats['skipped'] += 1

            # Wrap up
            mark_stale_jobs_closed(cursor, company_id)
            cursor.execute(
                "UPDATE company SET last_full_scrape_completed = CURRENT_TIMESTAMP WHERE id = %s",
                (company_id,)
            )
            cursor.execute("""
                INSERT INTO scrapinglog
                    (job_board, company_id, jobs_found, jobs_added, jobs_updated, jobs_skipped, errors, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed')
            """, (
                self.COMPANY_CONFIG['source_job_board'], company_id,
                stats['found'], stats['added'], stats['updated'], stats['skipped'],
                str(stats['errors']),
            ))

        return stats

    def cleanup(self):
        self.driver.quit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    conn = None
    scraper = None
    try:
        conn = get_database_connection()
        scraper = SodexoScraper(conn)
        logger.info("Starting Sodexo scrape")
        results = scraper.scrape_jobs()
        logger.info("=== SUMMARY ===")
        logger.info(f"Found:   {results['found']}")
        logger.info(f"Added:   {results['added']}")
        logger.info(f"Updated: {results['updated']}")
        logger.info(f"Skipped: {results['skipped']}")
        if results['errors']:
            logger.error(f"Errors ({len(results['errors'])}):")
            for e in results['errors']:
                logger.error(f"  {e}")
    except Exception as e:
        logger.error(f"Script failed: {e}")
        return 1
    finally:
        if scraper:
            scraper.cleanup()
        close_connection(conn)
    return 0


if __name__ == '__main__':
    exit(main())

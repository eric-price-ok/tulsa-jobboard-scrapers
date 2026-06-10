#!/usr/bin/env python3
"""
posting_operations.py
Centralized operations for job posting management
Handles creation, updates, and queries for job listings with caching support
"""

from typing import Dict, Optional, List, Any
import logging

logger = logging.getLogger(__name__)

def store_job_listing(cursor, job_data: Dict[str, Any], company_id: int, source_job_board: str = None) -> int:
    """
    Store new job listing with flexible field mapping

    Args:
        cursor: Database cursor
        job_data: Dictionary containing job data (any fields from joblistings table)
        company_id: ID of the company
        source_job_board: Name of the source job board (optional, can be in job_data)

    Returns:
        int: ID of the created job listing
    """

    # Required fields that must be provided
    required_fields = ['job_title']
    for field in required_fields:
        if field not in job_data or not job_data[field]:
            raise ValueError(f"Required field '{field}' is missing or empty")

    # Set default values for critical fields if not provided.
    # approved intentionally omitted — DB default is false; scraped jobs require manual review.
    defaults = {
        'job_status_id': None,  # Will be set via subquery
        'source_job_board': source_job_board,
        'company_id': company_id
    }

    # Merge defaults with provided data (job_data takes precedence)
    final_data = {**defaults, **job_data}

    # Remove None values and empty strings to let database defaults handle them
    final_data = {k: v for k, v in final_data.items()
                  if v is not None and v != ''}

    # Build dynamic INSERT statement
    fields = list(final_data.keys())
    placeholders = ['%s'] * len(fields)
    values = [final_data[field] for field in fields]

    # Special handling for job_status_id if not provided
    if 'job_status_id' not in final_data:
        fields.append('job_status_id')
        placeholders.append("(SELECT id FROM jobstatus WHERE name = 'active')")

    sql = f"""
    INSERT INTO joblistings ({', '.join(fields)})
    VALUES ({', '.join(placeholders)})
    RETURNING id
    """

    logger.info(f"Storing job: {final_data.get('job_title', 'Unknown Title')}")
    logger.debug(f"SQL: {sql}")
    logger.debug(f"Values: {values}")

    cursor.execute(sql, values)
    result = cursor.fetchone()
    job_id = result['id']

    logger.info(f"Created job listing with ID: {job_id}")
    return job_id

def check_existing_job_by_url(cursor, posting_url: str) -> Optional[int]:
    """
    Check if job already exists by URL, update timestamps if found

    Returns:
        int: Job ID if found, None if not found
    """
    cursor.execute("""
        SELECT id FROM joblistings
        WHERE posting_url = %s
    """, (posting_url,))

    existing = cursor.fetchone()
    if existing:
        cursor.execute("""
            UPDATE joblistings
            SET updated_at = CURRENT_TIMESTAMP,
                last_scraped = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (existing['id'],))
        logger.info(f"Job already exists (ID: {existing['id']}), updated timestamps")
        return existing['id']
    return None

def check_existing_job_by_hash(cursor, scraping_hash: str) -> Optional[int]:
    """
    Check if job already exists by scraping hash

    Returns:
        int: Job ID if found, None if not found
    """
    cursor.execute("""
        SELECT id FROM joblistings
        WHERE scraping_hash = %s
    """, (scraping_hash,))

    existing = cursor.fetchone()
    if existing:
        logger.info(f"Job already exists (ID: {existing['id']}) - duplicate hash")
        return existing['id']
    return None

def update_job_listing(cursor, job_id: int, job_data: Dict[str, Any]) -> bool:
    """
    Update existing job listing

    Args:
        cursor: Database cursor
        job_id: ID of job to update
        job_data: Dictionary containing fields to update

    Returns:
        bool: True if update successful
    """
    if not job_data:
        return False

    # Remove None values
    job_data = {k: v for k, v in job_data.items()
                if v is not None and v != ''}

    # Always update the updated_at timestamp
    job_data['updated_at'] = 'CURRENT_TIMESTAMP'

    # Build dynamic UPDATE statement
    set_clauses = []
    values = []

    for field, value in job_data.items():
        if field == 'updated_at':
            set_clauses.append(f"{field} = CURRENT_TIMESTAMP")
        else:
            set_clauses.append(f"{field} = %s")
            values.append(value)

    values.append(job_id)  # For WHERE clause

    sql = f"""
        UPDATE joblistings
        SET {', '.join(set_clauses)}
        WHERE id = %s
    """

    logger.info(f"Updating job ID: {job_id}")
    logger.debug(f"SQL: {sql}")
    logger.debug(f"Values: {values}")

    cursor.execute(sql, values)

    logger.info(f"Updated job listing ID: {job_id}")
    return True

def load_active_jobs_cache(cursor, company_id: int) -> Dict[str, int]:
    """
    Load and cache all active jobs for the company to reduce database reads

    Args:
        cursor: Database cursor
        company_id: ID of the company

    Returns:
        Dict[str, int]: Dictionary mapping posting_url to job_id
    """
    cursor.execute("""
        SELECT id, posting_url
        FROM joblistings
        WHERE company_id = %s
          AND job_status_id = (SELECT id FROM jobstatus WHERE name = 'active')
    """, (company_id,))

    results = cursor.fetchall()
    cache = {job['posting_url']: job['id'] for job in results if job['posting_url']}

    logger.info(f"Cached {len(cache)} active jobs for company {company_id}")
    return cache

def check_job_in_cache(posting_url: str, cache: Dict[str, int]) -> Optional[int]:
    """
    Check if job exists in cache by URL (WITHOUT updating timestamp)

    Args:
        posting_url: URL to check
        cache: Cache dictionary from load_active_jobs_cache

    Returns:
        int: Job ID if found in cache, None if not found
    """
    job_id = cache.get(posting_url)
    if job_id:
        logger.info(f"Job found in cache (ID: {job_id}) for URL: {posting_url}")
    return job_id

def update_job_verified_timestamp(cursor, job_id: int) -> bool:
    """
    Update timestamps for a single job that was verified to still exist

    Args:
        cursor: Database cursor
        job_id: Job ID to update

    Returns:
        bool: True if update successful
    """
    cursor.execute("""
        UPDATE joblistings
        SET updated_at = CURRENT_TIMESTAMP,
            last_scraped = CURRENT_TIMESTAMP
        WHERE id = %s
    """, (job_id,))
    logger.info(f"Updated timestamps for verified job ID: {job_id}")
    return True

def mark_stale_jobs_closed(cursor, company_id: int, logger=None):
    """Mark jobs as closed if not updated during this scrape cycle"""
    if logger is None:
        logger = logging.getLogger(__name__)

    # Get the last full scrape completion date
    cursor.execute("""
        SELECT last_full_scrape_completed
        FROM company
        WHERE id = %s
    """, (company_id,))

    company_data = cursor.fetchone()
    if not company_data or not company_data['last_full_scrape_completed']:
        logger.warning(f"No last_full_scrape_completed date found for company {company_id}")
        return 0

    last_scrape_date = company_data['last_full_scrape_completed']

    # Close jobs that weren't updated in this scrape cycle
    cursor.execute("""
        UPDATE joblistings SET
            job_status_id = (SELECT id FROM jobstatus WHERE name = 'closed'),
            date_closed = CURRENT_DATE
        WHERE company_id = %s
          AND job_status_id != (SELECT id FROM jobstatus WHERE name = 'closed')
          AND updated_at < %s
    """, (company_id, last_scrape_date))

    closed_count = cursor.rowcount
    if closed_count > 0:
        logger.info(f"Marked {closed_count} stale jobs as closed")

    return closed_count

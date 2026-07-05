#!/usr/bin/env python3
"""
Company Operations Utilities
Centralized company-related database operations
"""

import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _generate_slug(name: str) -> str:
    """Generate a URL-safe slug from a company name"""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def get_or_create_company(cursor, company_data: Dict) -> int:
    """
    Get existing company or create new one, return company ID

    Args:
        cursor: Database cursor
        company_data: Dict with keys: name, website, jobboard
                      Optional key: company_type_name (defaults to 'Private Company')

    Returns:
        int: Company ID
    """
    # Check if company exists
    cursor.execute(
        "SELECT id FROM company WHERE common_name = %s",
        (company_data['name'],)
    )
    result = cursor.fetchone()

    if result:
        logger.info(f"Found existing company: {company_data['name']} (ID: {result['id']})")
        return result['id']

    # Resolve company_type ID
    company_type_name = company_data.get('company_type_name', 'Private Company')
    cursor.execute(
        "SELECT id FROM company_type WHERE name = %s",
        (company_type_name,)
    )
    type_result = cursor.fetchone()
    if not type_result:
        raise ValueError(f"Company type '{company_type_name}' not found in database")
    company_type_id = type_result['id']

    # Generate a unique slug
    base_slug = _generate_slug(company_data['name'])
    slug = base_slug
    suffix = 1
    while True:
        cursor.execute("SELECT id FROM company WHERE slug = %s", (slug,))
        if not cursor.fetchone():
            break
        slug = f"{base_slug}-{suffix}"
        suffix += 1

    # Create new company. approved=True so the company is visible immediately;
    # is_scraped=True marks it as auto-created by a scraper for admin review.
    cursor.execute("""
        INSERT INTO company (slug, common_name, website, jobboard, approved, is_scraped, company_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        slug,
        company_data['name'],
        company_data.get('website'),
        company_data.get('jobboard'),
        True,
        True,
        company_type_id,
    ))

    result = cursor.fetchone()
    company_id = result['id']
    logger.info(f"Created new company: {company_data['name']} (ID: {company_id}, slug: {slug})")
    return company_id


def get_company_by_id(cursor, company_id: int) -> Optional[Dict]:
    """
    Retrieve company information by ID

    Args:
        cursor: Database cursor
        company_id: Company ID

    Returns:
        Dict or None: Company data
    """
    cursor.execute(
        "SELECT * FROM company WHERE id = %s",
        (company_id,)
    )
    result = cursor.fetchone()

    if result:
        logger.info(f"Retrieved company ID {company_id}: {result.get('common_name')}")
        return dict(result)
    else:
        logger.warning(f"Company ID {company_id} not found")
        return None


def get_company_by_name(cursor, company_name: str) -> Optional[Dict]:
    """
    Find company by name

    Args:
        cursor: Database cursor
        company_name: Company name to search for

    Returns:
        Dict or None: Company data
    """
    cursor.execute(
        "SELECT * FROM company WHERE common_name = %s",
        (company_name,)
    )
    result = cursor.fetchone()

    if result:
        logger.info(f"Found company '{company_name}' (ID: {result['id']})")
        return dict(result)
    else:
        logger.info(f"Company '{company_name}' not found")
        return None


def update_company_website(cursor, company_id: int, website: str) -> bool:
    """
    Update company website URL

    Args:
        cursor: Database cursor
        company_id: Company ID
        website: New website URL

    Returns:
        bool: True if updated successfully
    """
    try:
        cursor.execute("""
            UPDATE company
            SET website = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (website, company_id))

        if cursor.rowcount > 0:
            logger.info(f"Updated website for company ID {company_id}")
            return True
        else:
            logger.warning(f"No company found with ID {company_id}")
            return False

    except Exception as e:
        logger.error(f"Error updating company website: {e}")
        return False


def update_company_jobboard(cursor, company_id: int, jobboard_url: str) -> bool:
    """
    Update company job board URL

    Args:
        cursor: Database cursor
        company_id: Company ID
        jobboard_url: New job board URL

    Returns:
        bool: True if updated successfully
    """
    try:
        cursor.execute("""
            UPDATE company
            SET jobboard = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (jobboard_url, company_id))

        if cursor.rowcount > 0:
            logger.info(f"Updated job board URL for company ID {company_id}")
            return True
        else:
            logger.warning(f"No company found with ID {company_id}")
            return False

    except Exception as e:
        logger.error(f"Error updating company job board URL: {e}")
        return False


def get_company_config_by_name(cursor, company_name: str) -> Optional[Dict]:
    """
    Get company configuration data for scraping

    Args:
        cursor: Database cursor
        company_name: Name of company to look up

    Returns:
        Dict with name, website, jobboard keys or None if not found
    """
    cursor.execute("""
        SELECT id, common_name as name, website, jobboard
        FROM company
        WHERE common_name = %s
    """, (company_name,))

    result = cursor.fetchone()

    if result:
        logger.info(f"Retrieved company config for: {company_name} (ID: {result['id']})")
        return dict(result)
    else:
        logger.error(f"Company '{company_name}' not found in database")
        return None


def get_or_create_company_site(cursor, company_id: int, location_name: str, city_id: int = None, logger=None,
                                address1: str = None, state_id: int = None, country_id: int = None,
                                site_type_name: str = None) -> int:
    """Get existing company site by site_name or create new one, return site ID

    address1/state_id/country_id/site_type_name are only used when creating a
    new row — an existing site's address/type is never overwritten here.
    site_type_name is looked up against companysitetype (e.g. 'Branch Office').
    """
    if not location_name or not location_name.strip():
        return None

    site_name = location_name.strip()

    cursor.execute(
        "SELECT id FROM companysite WHERE company_id = %s AND LOWER(site_name) = LOWER(%s)",
        (company_id, site_name)
    )
    result = cursor.fetchone()

    if result:
        if logger:
            logger.info(f"Found existing company site: {site_name} (ID: {result['id']})")
        return result['id']

    site_type_id = None
    if site_type_name:
        cursor.execute("SELECT id FROM companysitetype WHERE name = %s", (site_type_name,))
        type_result = cursor.fetchone()
        if type_result:
            site_type_id = type_result['id']
        elif logger:
            logger.warning(f"Company site type '{site_type_name}' not found in database")

    cursor.execute("""
        INSERT INTO companysite (company_id, site_name, city_id, address1, state_id, country_id, site_type, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (company_id, site_name, city_id, address1, state_id, country_id, site_type_id, True))

    result = cursor.fetchone()
    site_id = result['id']
    if logger:
        logger.info(f"Created new company site: {site_name} (ID: {site_id})")
    return site_id

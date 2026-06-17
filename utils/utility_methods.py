#!/usr/bin/env python3
"""
Utility Methods
Common utility functions used across all scrapers
"""

import logging
import hashlib
import re
from typing import Dict, Optional

def setup_logging(company_name: str, log_level=logging.INFO):
    """
    Configure logging for scraper with company-specific log file
    
    Args:
        company_name: Name of the company (e.g., 'Melton Truck Lines')
        log_level: Logging level (default: logging.INFO)
    
    Returns:
        logger: Configured logger instance
    """
    # Create log filename: first 10 letters + _scraper.log
    clean_name = re.sub(r'[^a-zA-Z]', '', company_name.lower())[:10]
    
    # Ensure we have at least some characters for filename
    if len(clean_name) < 3:
        clean_name = 'company'  # fallback name
    
    log_filename = f"{clean_name}_scraper.log"
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured for {company_name} - Log file: {log_filename}")
    return logger


def normalize_work_location(value: str) -> Optional[str]:
    """
    Map various work location strings to the canonical name stored in the officelocations table.
    Returns the canonical name (e.g. 'On-site') or None if no match found.
    The caller is responsible for looking up the ID with that name.
    """
    if not value:
        return None

    lower = value.lower()

    mappings = {
        'On-site':  ['on-site', 'onsite', 'on site', 'office', 'in-person', 'in person', 'in office'],
        'Remote':   ['remote', 'fully remote', 'work from home', 'wfh', 'work-from-home'],
        'Hybrid':   ['hybrid'],
    }

    for canonical, variants in mappings.items():
        for variant in variants:
            if variant in lower:
                return canonical

    return None


def parse_salary_text(text: str) -> Dict:
    """
    Parse a salary string such as '$15.00 Hourly' or '$50,000 - $70,000 Annually'.
    Returns dict with minimum_salary, maximum_salary, and pay_frequency.
    pay_frequency values match the joblistings CHECK constraint:
      'hourly', 'daily', 'weekly', 'biweekly', 'monthly', 'annually'
    """
    if not text:
        return {'minimum_salary': None, 'maximum_salary': None, 'pay_frequency': None}

    amounts = re.findall(r'\$[\d,]+(?:\.\d+)?', text)
    parsed = []
    for a in amounts:
        try:
            parsed.append(float(a.replace('$', '').replace(',', '')))
        except ValueError:
            pass

    text_lower = text.lower()
    pay_frequency = None
    # Check biweekly before weekly and annually before monthly to avoid partial matches
    for canonical, variants in [
        ('biweekly', ['biweekly', 'bi-weekly']),
        ('annually', ['annually', 'annual', 'yearly']),
        ('monthly',  ['monthly']),
        ('weekly',   ['weekly']),
        ('daily',    ['daily']),
        ('hourly',   ['hourly']),
    ]:
        if any(v in text_lower for v in variants):
            pay_frequency = canonical
            break

    return {
        'minimum_salary': parsed[0] if parsed else None,
        'maximum_salary': parsed[1] if len(parsed) >= 2 else None,
        'pay_frequency': pay_frequency,
    }


def normalize_job_type(value: str) -> Optional[str]:
    """
    Map various job type strings to the canonical name stored in the jobtype table.
    Returns the canonical name (e.g. 'Full-time') or None if no match found.
    The caller is responsible for looking up the ID with that name.
    """
    if not value:
        return None

    lower = value.lower()

    mappings = {
        'Full-time':        ['full time', 'full-time', 'fulltime', 'full_time'],
        'Part-time':        ['part time', 'part-time', 'parttime', 'part_time'],
        'Contract':         ['contract', 'contractor'],
        'Contract-to-hire': ['contract to hire', 'contract-to-hire', 'c2h'],
        'Temporary':        ['temporary', 'temp'],
        'Internship':       ['intern', 'internship'],
        'As Needed':        ['as needed', 'prn', 'ecb'],
    }

    for canonical, variants in mappings.items():
        for variant in variants:
            if variant in lower:
                return canonical

    return None
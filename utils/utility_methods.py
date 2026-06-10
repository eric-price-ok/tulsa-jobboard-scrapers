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
        'Full-time':        ['full time', 'full-time', 'fulltime'],
        'Part-time':        ['part time', 'part-time', 'parttime'],
        'Contract':         ['contract', 'contractor'],
        'Contract-to-hire': ['contract to hire', 'contract-to-hire', 'c2h'],
        'Temporary':        ['temporary', 'temp'],
        'Internship':       ['intern', 'internship'],
    }

    for canonical, variants in mappings.items():
        for variant in variants:
            if variant in lower:
                return canonical

    return None
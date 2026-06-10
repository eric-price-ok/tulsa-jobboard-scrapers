#!/usr/bin/env python3
"""
Utility Methods
Common utility functions used across all scrapers
"""

import logging
import hashlib
import re
from typing import Dict

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
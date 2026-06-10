#!/usr/bin/env python3
"""
Date Utilities Module
Standardized date parsing and formatting functions for job scrapers
Handles common job board date formats and relative date expressions
"""

import re
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def parse_relative_date(posted_text: str) -> Optional[datetime]:
    """
    Parse relative date expressions like 'Posted 5 days ago' into actual datetime
    
    Handles formats:
    - "Posted 5 days ago"
    - "Posted 1 day ago" 
    - "5 days ago"
    - "1+ days ago"
    - "Posted 30+ Days Ago"
    
    Args:
        posted_text (str): Text containing relative date expression
        
    Returns:
        Optional[datetime]: Calculated date or None if parsing fails
    """
    if not posted_text or not isinstance(posted_text, str):
        return None
    
    try:
        # Clean the text - remove "Posted" prefix
        clean_text = re.sub(r'^Posted\s+', '', posted_text.strip(), flags=re.IGNORECASE)
        
        # Remove various "ago" suffixes
        clean_text = re.sub(r'\s*\+?\s*Days?\s+Ago$', '', clean_text, flags=re.IGNORECASE)
        clean_text = re.sub(r'\s*\+?\s*Day\s+Ago$', '', clean_text, flags=re.IGNORECASE)
        
        # Extract the number (handle "1+" format)
        clean_text = re.sub(r'\+', '', clean_text)
        
        # Handle "Today" and "Yesterday" keywords
        if 'today' in clean_text.lower():
            logger.debug(f"Parsed '{posted_text}' as today -> {datetime.now().strftime('%Y-%m-%d')}")
            return datetime.now()

        if 'yesterday' in clean_text.lower():
            result_date = datetime.now() - timedelta(days=1)
            logger.debug(f"Parsed '{posted_text}' as yesterday -> {result_date.strftime('%Y-%m-%d')}")
            return result_date

        # Convert to integer
        days_ago = int(clean_text.strip())
        
        # Calculate the date
        result_date = datetime.now() - timedelta(days=days_ago)
        
        logger.debug(f"Parsed '{posted_text}' as {days_ago} days ago -> {result_date.strftime('%Y-%m-%d')}")
        return result_date
        
    except (ValueError, TypeError) as e:
        logger.warning(f"Could not parse relative date: '{posted_text}' - {e}")
        return None

def format_date_for_db(date_obj: datetime, format_type: str = 'date') -> Optional[str]:
    """
    Format datetime object for database insertion
    
    Args:
        date_obj (datetime): Date to format
        format_type (str): 'date' for YYYY-MM-DD, 'datetime' for full timestamp
        
    Returns:
        Optional[str]: Formatted date string or None if input is invalid
    """
    if not date_obj or not isinstance(date_obj, datetime):
        return None
    
    try:
        if format_type == 'date':
            return date_obj.strftime('%Y-%m-%d')
        elif format_type == 'datetime':
            return date_obj.strftime('%Y-%m-%d %H:%M:%S')
        elif format_type == 'display':
            return date_obj.strftime('%m/%d/%Y')
        else:
            logger.warning(f"Unknown format_type: {format_type}, using default 'date'")
            return date_obj.strftime('%Y-%m-%d')
            
    except Exception as e:
        logger.error(f"Error formatting date {date_obj}: {e}")
        return None

def parse_workday_date(date_string: str) -> Optional[datetime]:
    """
    Parse various Workday date formats
    
    Common Workday formats:
    - "2024-01-15"
    - "Jan 15, 2024"
    - "January 15, 2024"
    - "01/15/2024"
    - "15-Jan-2024"
    
    Args:
        date_string (str): Date string from Workday
        
    Returns:
        Optional[datetime]: Parsed date or None if parsing fails
    """
    if not date_string or not isinstance(date_string, str):
        return None
    
    date_string = date_string.strip()
    
    # List of date formats to try
    date_formats = [
        '%Y-%m-%d',          # 2024-01-15
        '%m/%d/%Y',          # 01/15/2024
        '%d/%m/%Y',          # 15/01/2024 (European)
        '%b %d, %Y',         # Jan 15, 2024
        '%B %d, %Y',         # January 15, 2024
        '%d-%b-%Y',          # 15-Jan-2024
        '%d %b %Y',          # 15 Jan 2024
        '%Y/%m/%d',          # 2024/01/15
        '%m-%d-%Y',          # 01-15-2024
    ]
    
    for date_format in date_formats:
        try:
            parsed_date = datetime.strptime(date_string, date_format)
            logger.debug(f"Parsed '{date_string}' using format '{date_format}'")
            return parsed_date
        except ValueError:
            continue
    
    logger.warning(f"Could not parse Workday date: '{date_string}'")
    return None

def normalize_date_string(date_string: str) -> Optional[datetime]:
    """
    Handle multiple common date formats from various job boards
    
    This is a comprehensive parser that tries relative dates first,
    then absolute dates in various formats
    
    Args:
        date_string (str): Date string from any job board
        
    Returns:
        Optional[datetime]: Parsed date or None if parsing fails
    """
    if not date_string or not isinstance(date_string, str):
        return None
    
    date_string = date_string.strip()
    
    # First try relative date parsing
    if any(keyword in date_string.lower() for keyword in ['ago', 'posted', 'days']):
        relative_date = parse_relative_date(date_string)
        if relative_date:
            return relative_date
    
    # Try Workday date formats
    workday_date = parse_workday_date(date_string)
    if workday_date:
        return workday_date
    
    # Try some additional common formats
    additional_formats = [
        '%Y-%m-%dT%H:%M:%S',     # ISO format with time
        '%Y-%m-%dT%H:%M:%SZ',    # ISO format with timezone
        '%m/%d/%y',              # 01/15/24
        '%d.%m.%Y',              # 15.01.2024 (European)
        '%Y%m%d',                # 20240115
    ]
    
    for date_format in additional_formats:
        try:
            parsed_date = datetime.strptime(date_string, date_format)
            logger.debug(f"Parsed '{date_string}' using additional format '{date_format}'")
            return parsed_date
        except ValueError:
            continue
    
    logger.warning(f"Could not normalize date string: '{date_string}'")
    return None

def is_recent_date(date_obj: datetime, days_threshold: int = 30) -> bool:
    """
    Check if a date is within the specified number of days from today
    
    Useful for filtering old job postings
    
    Args:
        date_obj (datetime): Date to check
        days_threshold (int): Number of days to consider "recent"
        
    Returns:
        bool: True if date is recent, False otherwise
    """
    if not date_obj or not isinstance(date_obj, datetime):
        return False
    
    try:
        days_diff = (datetime.now() - date_obj).days
        return abs(days_diff) <= days_threshold
    except Exception as e:
        logger.error(f"Error checking if date is recent: {e}")
        return False

def calculate_days_ago(date_obj: datetime) -> Optional[int]:
    """
    Calculate how many days ago a date was from today
    
    Args:
        date_obj (datetime): Date to calculate from
        
    Returns:
        Optional[int]: Number of days ago, or None if calculation fails
    """
    if not date_obj or not isinstance(date_obj, datetime):
        return None
    
    try:
        days_diff = (datetime.now() - date_obj).days
        return max(0, days_diff)  # Don't return negative values for future dates
    except Exception as e:
        logger.error(f"Error calculating days ago: {e}")
        return None

def get_cutoff_date(days_back: int = 7) -> datetime:
    """
    Get a cutoff date for marking old jobs as expired
    
    Args:
        days_back (int): Number of days back from today
        
    Returns:
        datetime: Cutoff date
    """
    return datetime.now() - timedelta(days=days_back)

# Test functions for validation
def run_date_utility_tests():
    """
    Run basic tests on the date utility functions
    Call this to verify the module is working correctly
    """
    print("=== Date Utilities Test Suite ===\n")
    
    # Test relative date parsing
    print("Testing relative date parsing:")
    test_cases = [
        "Posted 5 days ago",
        "Posted 1 day ago", 
        "7 days ago",
        "Posted 30+ Days Ago",
        "1+ days ago",
        "Invalid date string"
    ]
    
    for test_case in test_cases:
        result = parse_relative_date(test_case)
        print(f"  '{test_case}' -> {result.strftime('%Y-%m-%d') if result else 'None'}")
    
    print()
    
    # Test Workday date parsing
    print("Testing Workday date parsing:")
    workday_cases = [
        "2024-01-15",
        "Jan 15, 2024",
        "01/15/2024",
        "15-Jan-2024",
        "Invalid workday date"
    ]
    
    for case in workday_cases:
        result = parse_workday_date(case)
        print(f"  '{case}' -> {result.strftime('%Y-%m-%d') if result else 'None'}")
    
    print()
    
    # Test date formatting
    print("Testing date formatting:")
    test_date = datetime(2024, 1, 15, 14, 30, 0)
    formats = ['date', 'datetime', 'display']
    
    for fmt in formats:
        result = format_date_for_db(test_date, fmt)
        print(f"  {fmt}: {result}")
    
    print()
    
    # Test utility functions
    print("Testing utility functions:")
    recent_date = datetime.now() - timedelta(days=5)
    old_date = datetime.now() - timedelta(days=50)
    
    print(f"  Is 5 days ago recent? {is_recent_date(recent_date)}")
    print(f"  Is 50 days ago recent? {is_recent_date(old_date)}")
    print(f"  Days ago for 5-day-old date: {calculate_days_ago(recent_date)}")
    print(f"  Cutoff date (7 days back): {get_cutoff_date().strftime('%Y-%m-%d')}")
    
    print("\n=== Test Complete ===")

if __name__ == "__main__":
    # Run tests when script is executed directly
    run_date_utility_tests()
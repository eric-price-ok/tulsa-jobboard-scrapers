#!/usr/bin/env python3
"""
Test script for date_utilities module
Run this to validate the date utility functions work correctly
"""

import sys
from datetime import datetime, timedelta

# Import the date utilities module
try:
    from date_utilities import (
        parse_relative_date, 
        format_date_for_db, 
        parse_workday_date,
        normalize_date_string,
        is_recent_date,
        calculate_days_ago,
        get_cutoff_date
    )
    print("✅ Successfully imported date_utilities module")
except ImportError as e:
    print(f"❌ Failed to import date_utilities: {e}")
    print("Make sure date_utilities.py is in the same directory as this test file")
    sys.exit(1)

def test_with_real_job_data():
    """Test with actual examples from your job scrapers"""
    
    print("\n=== Testing with Real Job Board Data ===")
    
    # Examples from OneOK/Williams scrapers
    real_examples = [
        "Posted 3 days ago",
        "Posted 1 day ago", 
        "Posted 7+ days ago",
        "Posted 30+ Days Ago",
        "5 days ago",
        "2024-01-15",
        "Jan 15, 2024",
        "01/15/2024"
    ]
    
    print("Relative date parsing:")
    for example in real_examples:
        result = parse_relative_date(example)
        if result:
            formatted = format_date_for_db(result)
            days_ago = calculate_days_ago(result)
            print(f"  '{example}' -> {formatted} ({days_ago} days ago)")
        else:
            print(f"  '{example}' -> Failed to parse")
    
    print("\nWorkday date parsing:")
    workday_examples = ["2024-01-15", "Jan 15, 2024", "01/15/2024", "15-Jan-2024"]
    for example in workday_examples:
        result = parse_workday_date(example)
        if result:
            formatted = format_date_for_db(result, 'display')
            print(f"  '{example}' -> {formatted}")
        else:
            print(f"  '{example}' -> Failed to parse")

def test_edge_cases():
    """Test edge cases and error conditions"""
    
    print("\n=== Testing Edge Cases ===")
    
    edge_cases = [
        None,
        "",
        "   ",
        "Invalid date string",
        "Posted yesterday",  # Not supported format
        "2024-13-45",       # Invalid date
        "99 days ago"       # Large number
    ]
    
    print("Edge case handling:")
    for case in edge_cases:
        result = normalize_date_string(case) if case else None
        print(f"  {repr(case)} -> {result}")

def test_database_integration():
    """Test formatting for database insertion"""
    
    print("\n=== Testing Database Integration ===")
    
    # Simulate getting a date from a job board and formatting for database
    job_posted_text = "Posted 5 days ago"
    
    # Parse the date
    parsed_date = parse_relative_date(job_posted_text)
    if parsed_date:
        # Format for different database needs
        db_date = format_date_for_db(parsed_date, 'date')
        db_datetime = format_date_for_db(parsed_date, 'datetime')
        display_date = format_date_for_db(parsed_date, 'display')
        
        print(f"Job posted: '{job_posted_text}'")
        print(f"  For database date field: {db_date}")
        print(f"  For database datetime field: {db_datetime}")
        print(f"  For user display: {display_date}")
        
        # Test if it's recent enough to scrape
        is_recent = is_recent_date(parsed_date, days_threshold=30)
        print(f"  Should we scrape this job? {is_recent}")
    
    # Test cutoff date for marking old jobs as expired
    cutoff = get_cutoff_date(days_back=7)
    print(f"\nCutoff date for marking jobs expired: {format_date_for_db(cutoff)}")

def main():
    """Run all tests"""
    print("🧪 Testing Date Utilities Module")
    print("=" * 50)
    
    try:
        # Run the built-in test suite first
        print("Running built-in test suite...")
        from date_utilities import run_date_utility_tests
        run_date_utility_tests()
        
        # Run additional tests
        test_with_real_job_data()
        test_edge_cases()
        test_database_integration()
        
        print("\n" + "=" * 50)
        print("✅ All tests completed successfully!")
        print("\nNext steps:")
        print("1. Update your scraper scripts to use these functions")
        print("2. Remove the duplicate date parsing code from each scraper")
        print("3. Test one scraper at a time to make sure it still works")
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
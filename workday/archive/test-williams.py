#!/usr/bin/env python3
"""
williams-filter-diagnostic.py
Test Williams job filtering logic - both initial filter and post-scrape validation
"""

import requests
import json
import time
from typing import Dict, List

def get_all_williams_jobs():
    """Get all Williams jobs using the working API configuration"""
    
    print("=== Getting All Williams Jobs ===")
    
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    })
    
    api_endpoint = 'https://williams.wd5.myworkdayjobs.com/wday/cxs/williams/External/jobs'
    
    # First try the exact working configuration from previous test
    working_configs = [
        {"limit": 20},  # Known to work
        {"limit": 50},  # Try higher
        {"limit": 89},  # Try to get all
        {"limit": 100}, # Try even higher
    ]
    
    for config in working_configs:
        print(f"Trying config: {json.dumps(config)}")
        
        try:
            response = session.post(
                api_endpoint,
                json=config,
                headers={
                    'Referer': 'https://williams.wd5.myworkdayjobs.com/External/',
                    'Origin': 'https://williams.wd5.myworkdayjobs.com'
                }
            )
            
            print(f"  Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                if 'jobPostings' in data:
                    jobs = data['jobPostings']
                    total = data.get('total', len(jobs))
                    print(f"  ✅ SUCCESS: Retrieved {len(jobs)} jobs (total available: {total})")
                    
                    # If we got fewer jobs than the total, try to get more
                    if len(jobs) < total and config['limit'] < total:
                        print(f"  📝 Got {len(jobs)} jobs but {total} available. Trying higher limit...")
                        continue
                    
                    return jobs
                else:
                    print(f"  ❌ No jobPostings in response: {list(data.keys())}")
            else:
                error_text = response.text[:200]
                print(f"  ❌ HTTP {response.status_code}: {error_text}")
                
        except Exception as e:
            print(f"  ❌ Error: {e}")
    
    # If all configs failed, try pagination approach
    print("\n=== Trying Pagination Approach ===")
    return get_jobs_with_pagination(session, api_endpoint)

def get_jobs_with_pagination(session, api_endpoint):
    """Get all jobs using pagination if single request fails"""
    
    all_jobs = []
    limit = 20  # Known working limit
    offset = 0
    
    while True:
        config = {"limit": limit, "offset": offset}
        print(f"Pagination request: {json.dumps(config)}")
        
        try:
            response = session.post(
                api_endpoint,
                json=config,
                headers={
                    'Referer': 'https://williams.wd5.myworkdayjobs.com/External/',
                    'Origin': 'https://williams.wd5.myworkdayjobs.com'
                }
            )
            
            if response.status_code == 200:
                data = response.json()
                if 'jobPostings' in data:
                    jobs = data['jobPostings']
                    total = data.get('total', 0)
                    
                    print(f"  ✅ Got {len(jobs)} jobs (offset {offset}, total {total})")
                    all_jobs.extend(jobs)
                    
                    # Check if we got all jobs
                    if len(jobs) == 0 or len(all_jobs) >= total:
                        break
                    
                    offset += limit
                    time.sleep(0.5)  # Be respectful
                else:
                    print(f"  ❌ No jobPostings in response")
                    break
            else:
                print(f"  ❌ HTTP {response.status_code}: {response.text[:200]}")
                break
                
        except Exception as e:
            print(f"  ❌ Error: {e}")
            break
    
    if all_jobs:
        print(f"✅ Pagination complete: Retrieved {len(all_jobs)} total jobs")
        return all_jobs
    else:
        print(f"❌ Pagination failed")
        return []

def test_initial_filter(jobs: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Test initial filtering logic and show what gets accepted/rejected"""
    
    print(f"\n=== Testing Initial Filter Logic ===")
    print(f"Input: {len(jobs)} total jobs")
    
    accepted = []
    rejected = []
    
    for i, job in enumerate(jobs, 1):
        title = job.get('title', 'No title')
        location = job.get('locationsText', 'No location')
        
        # Test filtering logic: "Tulsa" or "locations" (plural indicating multiple locations)
        is_accepted = False
        reason = ""
        
        if 'tulsa' in location.lower():
            is_accepted = True
            reason = "Contains 'Tulsa'"
        elif 'locations' in location.lower():
            is_accepted = True
            reason = "Contains 'locations' (multi-location job)"
        else:
            reason = "No Tulsa or locations indicator"
        
        result = {
            'job': job,
            'title': title,
            'location': location,
            'reason': reason
        }
        
        if is_accepted:
            accepted.append(result)
            print(f"✅ ACCEPT {i:2d}: {title}")
            print(f"    Location: {location}")
            print(f"    Reason: {reason}")
        else:
            rejected.append(result)
            print(f"❌ REJECT {i:2d}: {title}")
            print(f"    Location: {location}")
            print(f"    Reason: {reason}")
        
        print()
    
    print(f"📊 INITIAL FILTER RESULTS:")
    print(f"   Accepted: {len(accepted)} jobs")
    print(f"   Rejected: {len(rejected)} jobs")
    
    return accepted, rejected

def simulate_job_page_scraping(job_info: Dict) -> tuple[bool, str]:
    """Simulate scraping a job page and checking if it mentions Tulsa"""
    
    # In reality, this would use Selenium to scrape the job page
    # For this diagnostic, we'll simulate based on the job title and location
    
    title = job_info['title']
    location = job_info['location']
    
    # Simulate job page content analysis
    # We'll create realistic scenarios based on the location text
    
    if 'tulsa' in location.lower():
        # Jobs explicitly showing Tulsa location
        return True, "Job page confirms Tulsa location"
    
    elif 'locations' in location.lower():
        # Multi-location jobs - simulate checking if Tulsa is mentioned in job description
        
        # Simulate some realistic scenarios
        if any(keyword in title.lower() for keyword in ['analyst', 'corporate', 'manager', 'director']):
            # Corporate roles more likely to include Tulsa HQ
            return True, "Multi-location job, Tulsa mentioned in description (simulated corporate role)"
        
        elif any(keyword in title.lower() for keyword in ['technician', 'operator', 'field']):
            # Field roles less likely to include Tulsa
            return False, "Multi-location job, Tulsa NOT mentioned in description (simulated field role)"
        
        else:
            # Other roles - 50/50 chance for simulation
            import random
            if random.random() > 0.5:
                return True, "Multi-location job, Tulsa mentioned in description (simulated)"
            else:
                return False, "Multi-location job, Tulsa NOT mentioned in description (simulated)"
    
    else:
        # Single location jobs not in Tulsa
        return False, "Job page confirms non-Tulsa location"

def test_post_scrape_filter(initially_accepted: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Test post-scrape filtering and show what gets kept/rejected"""
    
    print(f"\n=== Testing Post-Scrape Filter Logic ===")
    print(f"Input: {len(initially_accepted)} jobs that passed initial filter")
    
    final_accepted = []
    post_rejected = []
    
    for i, job_info in enumerate(initially_accepted, 1):
        title = job_info['title']
        location = job_info['location']
        
        # Simulate scraping the job page
        is_tulsa_job, reason = simulate_job_page_scraping(job_info)
        
        result = {
            'job_info': job_info,
            'title': title,
            'location': location,
            'reason': reason
        }
        
        if is_tulsa_job:
            final_accepted.append(result)
            print(f"✅ KEEP {i:2d}: {title}")
            print(f"    Location: {location}")
            print(f"    Page Check: {reason}")
        else:
            post_rejected.append(result)
            print(f"❌ DROP {i:2d}: {title}")
            print(f"    Location: {location}")
            print(f"    Page Check: {reason}")
        
        print()
    
    print(f"📊 POST-SCRAPE FILTER RESULTS:")
    print(f"   Final Accepted: {len(final_accepted)} jobs")
    print(f"   Post-Scrape Rejected: {len(post_rejected)} jobs")
    
    return final_accepted, post_rejected

def show_summary(all_jobs: List[Dict], initially_accepted: List[Dict], 
                initially_rejected: List[Dict], final_accepted: List[Dict], 
                post_rejected: List[Dict]):
    """Show complete filtering summary"""
    
    print(f"\n" + "="*60)
    print(f"COMPLETE FILTERING SUMMARY")
    print(f"="*60)
    
    print(f"Total Williams Jobs: {len(all_jobs)}")
    print(f"└─ Initial Filter (Tulsa or 'locations'):")
    print(f"   ├─ Accepted: {len(initially_accepted)} jobs")
    print(f"   └─ Rejected: {len(initially_rejected)} jobs")
    print(f"      └─ Post-Scrape Filter (actual Tulsa check):")
    print(f"         ├─ Final Accepted: {len(final_accepted)} jobs")
    print(f"         └─ Rejected after scraping: {len(post_rejected)} jobs")
    
    print(f"\nEFFICIENCY ANALYSIS:")
    if len(initially_accepted) > 0:
        success_rate = len(final_accepted) / len(initially_accepted) * 100
        print(f"Success rate of initial filter: {success_rate:.1f}%")
        print(f"Jobs we'll scrape unnecessarily: {len(post_rejected)}")
    
    print(f"\nFINAL TULSA JOBS TO PROCESS:")
    for i, result in enumerate(final_accepted, 1):
        title = result['title']
        location = result['location']
        external_path = result['job_info']['job'].get('externalPath', '')
        
        if external_path:
            job_url = f"https://williams.wd5.myworkdayjobs.com/External{external_path}"
        else:
            job_url = "No URL available"
        
        print(f"{i:2d}. {title}")
        print(f"    Location: {location}")
        print(f"    URL: {job_url}")
        print()

def show_rejected_examples(initially_rejected: List[Dict]):
    """Show examples of jobs rejected by initial filter"""
    
    print(f"\n=== EXAMPLES OF INITIALLY REJECTED JOBS ===")
    print(f"(These jobs won't be scraped at all)")
    
    for i, result in enumerate(initially_rejected[:10], 1):  # Show first 10
        title = result['title']
        location = result['location']
        reason = result['reason']
        
        print(f"{i:2d}. {title}")
        print(f"    Location: {location}")
        print(f"    Rejected because: {reason}")
        print()
    
    if len(initially_rejected) > 10:
        print(f"... and {len(initially_rejected) - 10} more rejected jobs")

def main():
    """Run complete filtering diagnostic"""
    
    # Step 1: Get all Williams jobs
    all_jobs = get_all_williams_jobs()
    if not all_jobs:
        print("❌ Failed to get jobs, exiting")
        return
    
    # Step 2: Test initial filtering
    initially_accepted, initially_rejected = test_initial_filter(all_jobs)
    
    # Step 3: Test post-scrape filtering on accepted jobs
    final_accepted, post_rejected = test_post_scrape_filter(initially_accepted)
    
    # Step 4: Show complete summary
    show_summary(all_jobs, initially_accepted, initially_rejected, 
                final_accepted, post_rejected)
    
    # Step 5: Show examples of rejected jobs
    show_rejected_examples(initially_rejected)
    
    print(f"\n💡 RECOMMENDATION:")
    if len(final_accepted) > 0:
        print(f"✅ This filtering approach should work!")
        print(f"✅ We'll scrape {len(initially_accepted)} jobs and keep {len(final_accepted)} final jobs")
        if len(post_rejected) > 0:
            print(f"⚠️  We'll waste time scraping {len(post_rejected)} non-Tulsa jobs")
        print(f"\nAPI Configuration for main scraper:")
        print(f'body = {{"limit": {len(all_jobs) + 10}}}  # Get all jobs')
    else:
        print(f"❌ This filtering approach doesn't find any Tulsa jobs!")
        print(f"❌ Need to revise the filtering logic")

if __name__ == "__main__":
    main()
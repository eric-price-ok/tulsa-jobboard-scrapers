#!/usr/bin/env python3
"""
location_utilities.py
City matching and lookup utilities for filtering and tagging job listings by location.
"""

from typing import List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# Cities served by TulsaJobSpot (Tulsa metro and surrounding communities)
TULSA_METRO_CITIES = [
    'Tulsa',
    'Broken Arrow',
    'Owasso',
    'Bixby',
    'Jenks',
    'Sand Springs',
    'Sapulpa',
    'Claremore',
    'Catoosa',
    'Collinsville',
    'Skiatook',
    'Glenpool',
    'Coweta',
    'Wagoner',
    'Pryor',
    'Muskogee',
    'Bartlesville',
]


def find_served_city(location_text: str, served_cities: List[str] = None) -> Optional[str]:
    """
    Check if location_text contains any served city name (case-insensitive).

    Returns the canonical city name (from served_cities) or None if no match.
    Longer city names are checked first to avoid 'Sand' matching before 'Sand Springs'.
    """
    if not location_text:
        return None
    if served_cities is None:
        served_cities = TULSA_METRO_CITIES
    location_lower = location_text.lower()
    for city in sorted(served_cities, key=len, reverse=True):
        if city.lower() in location_lower:
            return city
    return None


def get_city_id(cursor, city_name: str) -> Optional[int]:
    """
    Look up city_id from the cities table by name, scoped to Oklahoma.
    Returns the city ID or None if not found.
    """
    cursor.execute("""
        SELECT c.id FROM cities c
        JOIN state s ON c.state_id = s.id
        WHERE c.city_name = %s AND s.name = 'Oklahoma'
    """, (city_name,))
    result = cursor.fetchone()
    if result:
        return result['id']
    logger.warning(f"City '{city_name}' not found in cities table")
    return None


def match_location_to_city_id(cursor, location_text: str, served_cities: List[str] = None) -> Tuple[Optional[str], Optional[int]]:
    """
    Match a location string against served cities and return (city_name, city_id).
    Returns (None, None) if no match or city not found in DB.

    Use this when you have a cursor available. Use find_served_city() alone when
    you only need to check membership (e.g. during job list filtering).
    """
    city_name = find_served_city(location_text, served_cities)
    if not city_name:
        return None, None
    city_id = get_city_id(cursor, city_name)
    return city_name, city_id

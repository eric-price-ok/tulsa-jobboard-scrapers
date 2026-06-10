#!/usr/bin/env python3
"""
Database Connection Utilities
Centralized database connection management with retry logic and error handling
Handles POSTGRES_PASSWORD environment variable and tulsajobspot database connection
"""

import psycopg
from psycopg.rows import dict_row
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

def get_database_connection(max_retries: int = 3, retry_delay: int = 5):
    """
    Establish database connection to tulsajobspot using POSTGRES_PASSWORD environment variable
    Returns configured psycopg connection with autocommit and dict_row factory
    """
    import os

    # Database configuration - works for both dev and production
    db_host = os.getenv('POSTGRES_HOST', 'localhost')
    db_port = os.getenv('POSTGRES_PORT', '5432')
    db_name = os.getenv('POSTGRES_DB', 'tulsajobspot')
    db_user = os.getenv('POSTGRES_USER', 'tulsajobspot')
    db_password = os.getenv('POSTGRES_PASSWORD')
    
    # Get password from environment variable
    db_password = os.getenv('POSTGRES_PASSWORD')
    if not db_password:
        raise ValueError("Please set POSTGRES_PASSWORD environment variable. Example: set POSTGRES_PASSWORD=your_password")
    
    connection_string = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    
    for attempt in range(max_retries):
        try:
            logger.info(f"Connecting to PostgreSQL database (attempt {attempt + 1}/{max_retries})")
            
            conn = psycopg.connect(connection_string, row_factory=dict_row)
            conn.autocommit = True
            
            logger.info("Connected to PostgreSQL database")
            return conn
            
        except Exception as e:
            logger.error(f"Failed to connect to database (attempt {attempt + 1}): {e}")
            
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("All connection attempts failed")
                raise

def test_connection() -> bool:
    """Test database connection without retries"""
    try:
        import os

        db_host = os.getenv('POSTGRES_HOST', 'localhost')
        db_port = os.getenv('POSTGRES_PORT', '5432')
        db_name = os.getenv('POSTGRES_DB', 'tulsajobspot')
        db_user = os.getenv('POSTGRES_USER', 'tulsajobspot')
        db_password = os.getenv('POSTGRES_PASSWORD')

        if not db_password:
            logger.error("✗ POSTGRES_PASSWORD environment variable not set")
            return False

        connection_string = f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"

        logger.info("Testing database connection...")
        conn = psycopg.connect(connection_string, row_factory=dict_row)
        conn.close()
        logger.info("✓ Database connection test successful")
        return True
    except Exception as e:
        logger.error(f"✗ Database connection test failed: {e}")
        return False

def close_connection(conn) -> None:
    """Safely close database connection"""
    if conn:
        try:
            conn.close()
            logger.info("Database connection closed")
        except Exception as e:
            logger.warning(f"Error closing database connection: {e}")

def execute_with_retry(conn, operation_func, max_retries: int = 3, *args, **kwargs):
    """
    Execute database operation with retry logic for connection issues
    operation_func should be a function that takes cursor as first argument
    """
    for attempt in range(max_retries):
        try:
            with conn.cursor() as cursor:
                return operation_func(cursor, *args, **kwargs)
                
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            logger.warning(f"Database operation failed (attempt {attempt + 1}): {e}")
            
            if attempt < max_retries - 1:
                logger.info("Retrying database operation...")
                time.sleep(2)
            else:
                logger.error("All operation retry attempts failed")
                raise
        except Exception as e:
            # Don't retry for non-connection errors
            logger.error(f"Database operation error (non-retryable): {e}")
            raise
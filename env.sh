#!/bin/bash
cd /home/deploy/tjs-scrapers
source venv/bin/activate
export $(grep POSTGRES_ /home/deploy/tulsajobspot/.env | xargs)
export POSTGRES_PORT=5433
export PYTHONPATH=/home/deploy/tjs-scrapers
echo "Scraper environment ready."

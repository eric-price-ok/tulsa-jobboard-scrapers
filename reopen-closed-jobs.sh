#!/bin/bash
# Reopen jobs that were incorrectly marked closed on or after a given date.
#
# Usage:
#   bash reopen-closed-jobs.sh <YYYY-MM-DD> <"Company Name">
#
# Examples:
#   bash reopen-closed-jobs.sh 2026-07-03 "Broken Arrow Public Schools"

if [ -z "$1" ] || [ -z "$2" ]; then
  echo "Usage: bash reopen-closed-jobs.sh <YYYY-MM-DD> \"Company Name\""
  exit 1
fi

SINCE_DATE="$1"
COMPANY_NAME="$2"

# Validate date format
if ! echo "$SINCE_DATE" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "Error: date must be in YYYY-MM-DD format (got: $SINCE_DATE)"
  exit 1
fi

PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2)

SQL="
WITH reopened AS (
  UPDATE joblistings
  SET job_status_id = (SELECT id FROM jobstatus WHERE name = 'active'),
      date_closed   = NULL,
      updated_at    = CURRENT_TIMESTAMP
  WHERE company_id = (SELECT id FROM company WHERE common_name = '$COMPANY_NAME')
    AND job_status_id = (SELECT id FROM jobstatus WHERE name = 'closed')
    AND date_closed >= '$SINCE_DATE'
  RETURNING id
)
SELECT COUNT(*) AS jobs_reopened FROM reopened;
"

echo "Reopening jobs closed on or after $SINCE_DATE for: $COMPANY_NAME"

docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$SQL"

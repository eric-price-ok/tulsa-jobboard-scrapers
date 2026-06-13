#!/bin/bash
# Usage:
#   bash delete-pending-jobs.sh                  # delete ALL pending jobs
#   bash delete-pending-jobs.sh "Greenheck"      # delete pending jobs for one company

PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2)

if [ -n "$1" ]; then
  SQL="DELETE FROM joblistings WHERE approved = false AND company_id = (SELECT id FROM company WHERE common_name = '$1');"
  echo "Deleting pending jobs for company: $1"
else
  SQL="DELETE FROM joblistings WHERE approved = false;"
  echo "Deleting ALL pending jobs..."
fi

docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$SQL"

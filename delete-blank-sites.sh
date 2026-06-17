#!/bin/bash
# Delete companysite records with a blank shortname for a given company.
#
# Usage:
#   bash delete-blank-sites.sh "Laureate Psychiatric Clinic"
#   bash delete-blank-sites.sh "Saint Francis Hospital South"

if [ -z "$1" ]; then
  echo "Usage: bash delete-blank-sites.sh \"Company Name\""
  exit 1
fi

COMPANY="$1"
PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2)

# Preview what will be deleted first
PREVIEW_SQL="
SELECT cs.id, cs.shortname, cs.city_id, ci.city_name
FROM companysite cs
LEFT JOIN cities ci ON ci.id = cs.city_id
WHERE cs.company_id = (SELECT id FROM company WHERE common_name = '$COMPANY')
  AND (cs.shortname IS NULL OR TRIM(cs.shortname) = '');
"

echo "Company: $COMPANY"
echo "Blank site records that will be deleted:"
docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$PREVIEW_SQL"

read -rp "Proceed with deletion? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

DELETE_SQL="
DELETE FROM companysite
WHERE company_id = (SELECT id FROM company WHERE common_name = '$COMPANY')
  AND (shortname IS NULL OR TRIM(shortname) = '');
"

docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$DELETE_SQL"

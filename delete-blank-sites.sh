#!/bin/bash
# Delete companysite records with a blank site_name for a given company.
# These are records created by an old scraper bug that wrote to the wrong
# column (shortname instead of site_name). Safe to delete — any record the
# admin created will have site_name populated.
#
# Usage:
#   bash delete-blank-sites.sh "Warren Clinic"
#   bash delete-blank-sites.sh "Saint Francis Hospital South"

if [ -z "$1" ]; then
  echo "Usage: bash delete-blank-sites.sh \"Company Name\""
  exit 1
fi

COMPANY="$1"
PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2)

# Show ALL sites for the company so you can see what's real vs scraper-created
LIST_SQL="
SELECT cs.id,
       cs.site_name,
       cs.shortname,
       ci.city_name,
       CASE WHEN (cs.site_name IS NULL OR TRIM(cs.site_name) = '') THEN 'DELETE' ELSE 'keep' END AS action
FROM companysite cs
LEFT JOIN cities ci ON ci.id = cs.city_id
WHERE cs.company_id = (SELECT id FROM company WHERE common_name = '$COMPANY')
ORDER BY action DESC, cs.id;
"

echo "Company: $COMPANY"
echo "All site records (DELETE = blank site_name, would be removed):"
docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$LIST_SQL"

read -rp "Delete all rows marked DELETE above? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

DELETE_SQL="
DELETE FROM companysite
WHERE company_id = (SELECT id FROM company WHERE common_name = '$COMPANY')
  AND (site_name IS NULL OR TRIM(site_name) = '');
"

docker exec \
  -e PGPASSWORD="$PGPASSWORD" \
  tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "$DELETE_SQL"

#!/bin/bash
docker exec \
  -e PGPASSWORD=$(grep POSTGRES_PASSWORD /home/deploy/tulsajobspot/.env | cut -d= -f2) \
  -it tulsajobspot-db-1 psql -U tulsajobspot -d tulsajobspot \
  -c "DELETE FROM joblistings WHERE approved = false;"

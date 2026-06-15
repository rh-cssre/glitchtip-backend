#!/usr/bin/env bash
set -e

echo "Run Django migrations"
if [ -n "$MAINTENANCE_DATABASE_URL" ]; then
    DB_FLAG="--database maintenance"
else
    DB_FLAG=""
fi
./manage.py migrate --skip-checks $DB_FLAG
echo "Create Django cache table, if needed"
./manage.py createcachetable
echo "Create and delete Postgres partitions"
./manage.py maintain_partitions

#!/usr/bin/env bash
set -e

echo "Run Django migrations"
./manage.py migrate --skip-checks
echo "Create and delete Postgres partitions"
./manage.py pgpartition --yes

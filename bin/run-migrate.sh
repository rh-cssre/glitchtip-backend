#!/usr/bin/env bash
set -e

echo "Run Django migrations"
./manage.py migrate
echo "Create and delete Postgres partitions"
./manage.py pgpartition --yes
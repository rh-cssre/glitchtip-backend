#!/usr/bin/env bash
set -e

bin/run-migrate.sh
./manage.py runserver 0.0.0.0:8080

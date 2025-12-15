#!/usr/bin/env bash
set -e

bin/run-migrate.sh
exec ./manage.py runserver 0.0.0.0:8080

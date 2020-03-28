#!/usr/bin/env bash
set -e

./manage.py migrate && ./manage.py runserver 0.0.0.0:8080

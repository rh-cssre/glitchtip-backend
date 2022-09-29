#!/usr/bin/env bash
set -e

exec celery -A glitchtip worker -l info -s /code/var/run/celerybeat-schedule --uid=app --gid=app

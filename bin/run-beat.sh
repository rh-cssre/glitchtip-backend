#!/usr/bin/env bash
set -e

exec celery -A glitchtip beat -s /code/var/run/celerybeat-schedule -l info --uid=app --gid=app --pidfile=
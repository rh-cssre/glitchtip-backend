#!/usr/bin/env bash
set -e

exec celery -A glitchtip beat -s /celery/celerybeat-schedule -l info --uid=app --gid=app --pidfile=
#!/usr/bin/env bash
set -e

exec celery -A glitchtip worker -l info -s /celery/celerybeat-schedule

#!/usr/bin/env bash
export IS_CELERY="true"
set -e

exec celery -A glitchtip worker -l info -B -s /tmp/celerybeat-schedule

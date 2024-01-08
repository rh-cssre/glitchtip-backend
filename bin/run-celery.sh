#!/usr/bin/env bash
export IS_CELERY="true"
export CELERY_SKIP_CHECKS="true"
set -e

exec celery -A glitchtip worker -l info -s /tmp/celerybeat-schedule

#!/usr/bin/env bash
export IS_CELERY="true"
export CELERY_SKIP_CHECKS="true"
set -e

exec celery -A glitchtip beat -s /tmp/celerybeat-schedule -l info --pidfile=
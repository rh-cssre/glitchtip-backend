#!/usr/bin/env bash
export IS_CELERY="true"
export C_FORCE_ROOT="true"
set -e

exec celery -A glitchtip worker -l info -B -s /tmp/celerybeat-schedule

#!/usr/bin/env bash
export IS_CELERY="true"
export CELERY_SKIP_CHECKS="true"

CELERY_WORKER_POOL="${CELERY_WORKER_POOL:-'threads'}"
CELERY_WORKER_CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-10}"
CELERY_WORKER_PREFETCH_MULTIPLIER="${CELERY_WORKER_PREFETCH_MULTIPLIER:-11}"

set -e

exec celery -A glitchtip worker -l info -B -s /tmp/celerybeat-schedule

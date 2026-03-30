#!/usr/bin/env bash
export IS_WORKER="true"
export LOG_LEVEL=${LOG_LEVEL:-INFO}
set -e

. "$(dirname "$0")/tune-malloc.sh"

exec ./manage.py runworker --scheduler

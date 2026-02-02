#!/usr/bin/env bash
export IS_WORKER="true"
export LOG_LEVEL=${LOG_LEVEL:-INFO}
set -e

exec ./manage.py runworker --scheduler
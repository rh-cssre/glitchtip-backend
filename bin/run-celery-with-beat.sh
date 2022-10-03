#!/usr/bin/env bash
set -e

exec celery -A glitchtip worker -l info -B -s /tmp/celerybeat-schedule

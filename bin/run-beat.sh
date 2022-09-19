#!/usr/bin/env bash
set -e

exec celery -A glitchtip beat -s /tmp/celerybeat-schedule -l info --pidfile=
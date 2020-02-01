#!/usr/bin/env bash
set -e

exec celery -A glitchtip beat -l info --pidfile=
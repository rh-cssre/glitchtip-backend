#!/usr/bin/env sh
set -e

# Pre-deploy entrypoint: the same initialization the server runs on start, so
# running it here and setting SKIP_INIT keeps schema changes off the serving
# path. Both take the same lock, so the two can overlap harmlessly.
exec ./manage.py init_database

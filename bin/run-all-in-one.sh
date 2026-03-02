#!/usr/bin/env sh
set -e

# Run initialization commands unless SKIP_INIT is set
# Set SKIP_INIT=true when running migrations as a pre-deploy hook
if [ "${SKIP_INIT}" != "True" ] && [ "${SKIP_INIT}" != "true" ] && [ "${SKIP_INIT}" != "1" ]; then
    python manage.py migrate --no-input --skip-checks
    python manage.py maintain_partitions

    if [ "$GLITCHTIP_BOOTSTRAP_DEV" = "True" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "true" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "1" ]; then
        python manage.py bootstrap_dev
    fi

    # Create cache table if django.contrib.sessions is installed
    python -c "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'glitchtip.settings'); django.setup(); from django.conf import settings; from django.core.management import call_command; call_command('createcachetable') if 'django.contrib.sessions' in settings.INSTALLED_APPS else None"
fi

# Enable embedded worker
export GLITCHTIP_EMBED_WORKER=true

# Granian settings

WORKERS=${GRANIAN_WORKERS:-${WEB_CONCURRENCY:-1}}

HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-${PORT:-8000}}

G_LOG_LEVEL=${GRANIAN_LOG_LEVEL:-INFO}

# Serve static files by default if the directory exists
if [ -n "$GRANIAN_STATIC_PATH_MOUNT" ]; then
    export GRANIAN_STATIC_PATH_MOUNT
elif [ -d "static" ]; then
    export GRANIAN_STATIC_PATH_MOUNT="static"
fi

if [ "${ENABLE_OBSERVABILITY_API}" = "True" ] || [ "${ENABLE_OBSERVABILITY_API}" = "true" ] || [ "${ENABLE_OBSERVABILITY_API}" = "1" ]; then
    if [ "$WORKERS" -gt 1 ]; then
        export PROMETHEUS_MULTIPROC_DIR=${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus_multiproc}
        mkdir -p $PROMETHEUS_MULTIPROC_DIR
        rm -rf $PROMETHEUS_MULTIPROC_DIR/*
    fi
fi

# Run Granian

exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $G_LOG_LEVEL --no-ws "$@"

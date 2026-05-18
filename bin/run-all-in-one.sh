#!/usr/bin/env sh
set -e

. "$(dirname "$0")/tune-malloc.sh"

# Run initialization commands unless SKIP_INIT is set
# Set SKIP_INIT=true when running migrations as a pre-deploy hook
if [ "${SKIP_INIT}" != "True" ] && [ "${SKIP_INIT}" != "true" ] && [ "${SKIP_INIT}" != "1" ]; then
    if [ -n "$MAINTENANCE_DATABASE_URL" ]; then
        DB_FLAG="--database maintenance"
    else
        DB_FLAG=""
    fi
    python manage.py migrate --no-input --skip-checks $DB_FLAG
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

# Worker memory limit: restart workers that grow too large (likely fragmentation).
# Granian spawns a new worker before terminating the old one, so traffic is not interrupted.
# Skip if the operator has already set GRANIAN_WORKERS_MAX_RSS.
if [ -z "$GRANIAN_WORKERS_MAX_RSS" ]; then
    MEM_LIMIT_MB=0
    if [ -f /sys/fs/cgroup/memory.max ]; then
        MEM_LIMIT_BYTES=$(cat /sys/fs/cgroup/memory.max)
        if [ "$MEM_LIMIT_BYTES" != "max" ] 2>/dev/null; then
            MEM_LIMIT_MB=$(( MEM_LIMIT_BYTES / 1048576 ))
        fi
    fi
    if [ "$MEM_LIMIT_MB" -gt 0 ] 2>/dev/null; then
        # Use 50% of the cgroup limit, but no less than 1024 MiB.
        # 50% leaves headroom for the brief overlap when granian runs
        # both the old and new worker during a respawn.
        RSS_LIMIT=$(( MEM_LIMIT_MB / 2 ))
        if [ "$RSS_LIMIT" -lt 1024 ]; then
            RSS_LIMIT=1024
        fi
    else
        RSS_LIMIT=2048
    fi
    export GRANIAN_WORKERS_MAX_RSS=$RSS_LIMIT
fi

# Run Granian

exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $G_LOG_LEVEL --no-ws "$@"

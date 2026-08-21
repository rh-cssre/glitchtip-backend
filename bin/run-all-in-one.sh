#!/usr/bin/env sh
set -e

# INFO by default: this is the only serving entrypoint, and the settings
# default (WARNING) is meant for management commands.
export LOG_LEVEL=${LOG_LEVEL:-INFO}

. "$(dirname "$0")/tune-malloc.sh"

# Initialize the database unless SKIP_INIT is set. Safe to run from every
# replica at once — init_database takes an advisory lock, so a second process
# waits for the first rather than racing it on DDL. Set SKIP_INIT=true when
# migrations run as a pre-deploy step (bin/run-migrate.sh) instead.
if [ "${SKIP_INIT}" != "True" ] && [ "${SKIP_INIT}" != "true" ] && [ "${SKIP_INIT}" != "1" ]; then
    python manage.py init_database

    if [ "$GLITCHTIP_BOOTSTRAP_DEV" = "True" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "true" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "1" ]; then
        python manage.py bootstrap_dev
    fi
fi

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

# Bound the respawn overlap. Granian's kill timeout is disabled by default,
# and it both waits on the old worker forever and skips the SIGKILL escalation
# when unset -- so a worker that doesn't stop on SIGTERM keeps its memory
# alongside its replacement until the cgroup OOM killer resolves it. That turns
# the RSS recycle above into the thing that triggers the OOM. 60s is well beyond
# a normal in-flight drain while still capping the window at 2x RSS.
export GRANIAN_WORKERS_KILL_TIMEOUT=${GRANIAN_WORKERS_KILL_TIMEOUT:-60}

# Run Granian

exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $G_LOG_LEVEL --no-ws "$@"

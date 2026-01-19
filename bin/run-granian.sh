#!/usr/bin/env sh
set -e

# Workers: support both Heroku-style WEB_CONCURRENCY and GRANIAN_WORKERS
# GRANIAN_WORKERS takes precedence if set
WORKERS=${GRANIAN_WORKERS:-${WEB_CONCURRENCY:-1}}

# Host and port with defaults (could also just use GRANIAN_HOST/GRANIAN_PORT directly)
HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-8000}

# Use async by default
USE_ASYNC_SERVER=${USE_ASYNC_SERVER:-true}

# Serve static files by default if the directory exists
# If GRANIAN_STATIC_PATH_MOUNT is explicitly set, we respect it (and let Granian fail if it's missing)
# If it's NOT set, we check for the default 'static' directory.
# If 'static' exists, we set it. If not, we skip it (falling back to Whitenoise or no static serving).
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

if [ "$USE_ASYNC_SERVER" = "true" ]; then
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (ASGI)"
    exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS "$@"
else
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (WSGI)"
    exec granian --interface wsgi glitchtip.wsgi:application --host $HOST --port $PORT --workers $WORKERS "$@"
fi

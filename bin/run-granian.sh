#!/usr/bin/env sh
set -e

# Workers: support both Heroku-style WEB_CONCURRENCY and GRANIAN_WORKERS
# GRANIAN_WORKERS takes precedence if set
WORKERS=${GRANIAN_WORKERS:-${WEB_CONCURRENCY:-1}}

# Host and port with defaults (could also just use GRANIAN_HOST/GRANIAN_PORT directly)
HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-${PORT:-8000}}

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

# Determine ASGI interface mode.
# MCP requires lifespan for its Starlette session manager.
# When MCP is disabled, use asginl (no lifespan) since Django doesn't support it.
if [ "$GLITCHTIP_ENABLE_MCP" = "True" ] || [ "$GLITCHTIP_ENABLE_MCP" = "true" ] || [ "$GLITCHTIP_ENABLE_MCP" = "1" ]; then
    INTERFACE="asgi"
else
    INTERFACE="asginl"
fi

echo "Start GlitchTip with ${WORKERS} granian worker(s) (${INTERFACE})"
exec granian --interface $INTERFACE glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --no-ws "$@"

#!/usr/bin/env sh
set -e

WORKERS=${WEB_CONCURRENCY:-1}
HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-8000}
LOG_LEVEL=${GRANIAN_LOG_LEVEL:-info}

if [ "$USE_ASYNC_SERVER" = "true" ]; then
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (ASGI)"
    exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $LOG_LEVEL
else
    echo "Start GlitchTip with ${WORKERS} granian worker(s) (WSGI)"
    exec granian --interface wsgi glitchtip.wsgi:application --host $HOST --port $PORT --workers $WORKERS --blocking-threads $WORKERS --log-level $LOG_LEVEL
fi

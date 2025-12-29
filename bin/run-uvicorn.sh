#!/usr/bin/env sh
set -e

WORKERS=${WEB_CONCURRENCY:-1}
HOST=${UVICORN_HOST:-0.0.0.0}
PORT=${UVICORN_PORT:-8000}
LOG_LEVEL=${UVICORN_LOG_LEVEL:-info}

echo "Start GlitchTip with ${WORKERS} uvicorn worker(s)"

exec uvicorn glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $LOG_LEVEL --lifespan off

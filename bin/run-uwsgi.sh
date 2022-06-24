#!/usr/bin/env sh
set -e

UWSGI_LISTEN="${UWSGI_LISTEN:-128}"
PORT="${PORT:-8000}"
CHEAPER_OVERLOAD="${UWSGI_CHEAPER_OVERLOAD:-30}"

exec uwsgi \
    --module=glitchtip.wsgi:application \
    --env DJANGO_SETTINGS_MODULE=glitchtip.settings \
    --master --pidfile=/tmp/project-master.pid \
    --log-x-forwarded-for \
    --log-format-strftime \
    --http-socket=:$PORT \
    --cheaper-algo=busyness \
    --cheaper-overload=$CHEAPER_OVERLOAD \
    --cheaper-step=1 \
    --cheaper-busyness-max=50 \
    --cheaper-busyness-min=25 \
    --cheaper-busyness-multiplier=20 \
    --harakiri=60 \
    --max-requests=10000 \
    --die-on-term \
    --enable-threads \
    --single-interpreter \
    --post-buffering \
    --buffer-size=83146 \
    --ignore-sigpipe \
    --ignore-write-errors \
    --disable-write-exception \
    --listen=$UWSGI_LISTEN

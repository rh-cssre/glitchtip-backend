#!/bin/sh
uwsgi \
    --module=glitchtip.wsgi:application \
    --env DJANGO_SETTINGS_MODULE=glitchtip.settings \
    --master --pidfile=/tmp/project-master.pid \
    --http-socket=:$PORT \
    --processes=8 \
    --harakiri=300 \
    --max-requests=5000 \
    --die-on-term \
    --enable-threads \
    --single-interpreter \
    --post-buffering \
    --buffer-size=83146


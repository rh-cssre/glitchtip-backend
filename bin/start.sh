#!/usr/bin/env sh
set -e

SERVER_ROLE="${SERVER_ROLE:-web}"

case $SERVER_ROLE in
    web)
        SCRIPT="./bin/run-uwsgi.sh"
        ;;
    worker)
        SCRIPT="./bin/run-celery.sh"
        ;;
    beat)
        SCRIPT="./bin/run-beat.sh"
        ;;
    *)
        echo "Unknown server role provided: $SERVER_ROLE. Should be web|worker|beat."
        exit 1
        ;;
esac

. "$SCRIPT"

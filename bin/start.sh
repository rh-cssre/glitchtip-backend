#!/usr/bin/env sh
set -e

SERVER_ROLE="${SERVER_ROLE:-web}"
HEROKU_DYNO="${DYNO:-no}"

case "$HEROKU_DYNO" in
    web*) ./manage.py migrate ;;
    worker*) SERVER_ROLE=worker ;;
esac

case $SERVER_ROLE in
    web)
        SCRIPT="./bin/run-web.sh"
        ;;
    worker)
        SCRIPT="./bin/run-worker.sh"
        ;;
    worker_with_beat)
        SCRIPT="./bin/run-worker.sh"
        ;;
    *)
        echo "Unknown server role provided: $SERVER_ROLE. Should be web|worker|beat."
        exit 1
        ;;
esac

. "$SCRIPT"

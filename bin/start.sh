#!/usr/bin/env sh
set -e

SERVER_ROLE="${SERVER_ROLE:-web}"
if [ "$SERVER_ROLE" = "web" ] && [ "${GLITCHTIP_EMBED_WORKER}" = "true" ]; then
    SERVER_ROLE="all_in_one"
fi
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
    all_in_one)
        SCRIPT="./bin/run-all-in-one.sh"
        ;;
    *)
        echo "Unknown server role provided: $SERVER_ROLE. Should be web|worker|all_in_one."
        exit 1
        ;;
esac

. "$SCRIPT"

#!/usr/bin/env sh
set -e

# GlitchTip runs as one process: a granian ASGI server that also hosts the
# task worker and the scheduler. There are no roles to dispatch on — scale by
# running more of this same process.
#
# A role left over from an older deployment is refused rather than ignored: a
# deployment that asked for a worker would otherwise silently start serving
# HTTP and running migrations instead.
case "${SERVER_ROLE:-web}" in
    web|all_in_one) ;;
    *)
        echo "SERVER_ROLE=$SERVER_ROLE is no longer supported: GlitchTip runs one" >&2
        echo "process that serves HTTP and consumes the task queue. Unset" >&2
        echo "SERVER_ROLE and run a single deployment of ./bin/start.sh." >&2
        exit 1
        ;;
esac

# Heroku picked the role from the dyno name rather than SERVER_ROLE, so a
# worker dyno needs the same refusal: without it, it silently becomes a second
# web server that also migrates.
case "${DYNO:-}" in
    worker*)
        echo "The $DYNO dyno cannot run a worker: GlitchTip runs one process" >&2
        echo "that serves HTTP and consumes the task queue. Scale the web dyno" >&2
        echo "and remove this one." >&2
        exit 1
        ;;
esac

. ./bin/run-all-in-one.sh

#!/usr/bin/env sh
set -e

# INFO by default: this is the only serving entrypoint, and the settings
# default (WARNING) is meant for management commands.
export LOG_LEVEL=${LOG_LEVEL:-INFO}

. "$(dirname "$0")/tune-malloc.sh"

# Initialize the database unless SKIP_INIT is set. Safe to run from every
# replica at once — init_database takes an advisory lock, so a second process
# waits for the first rather than racing it on DDL. Set SKIP_INIT=true when
# migrations run as a pre-deploy step (bin/run-migrate.sh) instead.
if [ "${SKIP_INIT}" != "True" ] && [ "${SKIP_INIT}" != "true" ] && [ "${SKIP_INIT}" != "1" ]; then
    python manage.py init_database

    if [ "$GLITCHTIP_BOOTSTRAP_DEV" = "True" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "true" ] || [ "$GLITCHTIP_BOOTSTRAP_DEV" = "1" ]; then
        python manage.py bootstrap_dev
    fi
fi

# Granian settings

WORKERS=${GRANIAN_WORKERS:-${WEB_CONCURRENCY:-1}}

HOST=${GRANIAN_HOST:-0.0.0.0}
PORT=${GRANIAN_PORT:-${PORT:-8000}}

G_LOG_LEVEL=${GRANIAN_LOG_LEVEL:-INFO}

# Static files are served by ServeStatic inside the ASGI app (glitchtip/asgi.py).
# It negotiates the precompressed .gz/.br variants collectstatic writes and sets
# cache headers per file; granian's own mount can do neither -- it has no
# compression support and applies one expiry to the whole tree.
#
# An operator who would rather granian serve them can still set
# GRANIAN_STATIC_PATH_MOUNT explicitly: granian then answers those requests
# first and ServeStatic never sees them. That trades compression and per-file
# caching for granian's raw throughput, which only matters without a CDN in
# front and with a cold client cache.
if [ -n "$GRANIAN_STATIC_PATH_MOUNT" ]; then
    export GRANIAN_STATIC_PATH_MOUNT
fi

if [ "${ENABLE_OBSERVABILITY_API}" = "True" ] || [ "${ENABLE_OBSERVABILITY_API}" = "true" ] || [ "${ENABLE_OBSERVABILITY_API}" = "1" ]; then
    if [ "$WORKERS" -gt 1 ]; then
        export PROMETHEUS_MULTIPROC_DIR=${PROMETHEUS_MULTIPROC_DIR:-/tmp/prometheus_multiproc}
        mkdir -p $PROMETHEUS_MULTIPROC_DIR
        rm -rf $PROMETHEUS_MULTIPROC_DIR/*
    fi
fi

# Worker recycling is off unless an operator asks for it.
#
# Granian can respawn a worker when its resident memory crosses a threshold
# (GRANIAN_WORKERS_MAX_RSS) or when it reaches an age
# (GRANIAN_WORKERS_LIFETIME). This image sets neither.
#
# A memory-triggered respawn fires at peak RSS by definition: granian starts
# the replacement while the old worker is still serving, so the instant the
# process is at its largest is the instant a second copy appears beside it.
# Under a container memory limit that overlap is what invokes the OOM killer,
# and the killed worker takes its in-flight work with it -- an interrupted
# worker loses whatever task batch it was draining.
#
# Memory is also the wrong signal for this workload. Resident memory is
# dominated by bursts that scale with event volume and worker concurrency, not
# by a leak: the working set climbs while a batch is in flight and falls back to
# its floor when the batch completes. An RSS threshold cannot tell a busy worker
# apart from a growing one, so it recycles the healthy case. An install small
# enough to care about a tight memory budget is also small enough that those
# bursts never happen.
#
# Operators who do want a backstop should prefer age over memory:
#
#     GRANIAN_WORKERS_LIFETIME=86400    # seconds; respawn on age
#
# An age-triggered respawn lands at whatever the worker's memory happens to be
# when the timer expires, which on average is its average footprint rather than
# its peak, so the headroom it needs is roughly one more typical worker instead
# of twice a threshold.
#
# To bound growth by memory anyway, pair the threshold with a sample count --
# granian samples every 30s (GRANIAN_RSS_SAMPLE_INTERVAL) and by default
# respawns after a single sample over the line, which makes any burst a trigger:
#
#     GRANIAN_WORKERS_MAX_RSS=2048      # MiB, per worker
#     GRANIAN_RSS_SAMPLES=10            # consecutive samples over the threshold
#
# Either way, leave room for one extra worker alongside the one being replaced.

# Bound the respawn overlap whenever a respawn does happen (an operator opting
# into the settings above, or a rolling restart). Granian's kill timeout is
# disabled by default, and it both waits on the old worker forever and skips the
# SIGKILL escalation when unset -- so a worker that doesn't stop on SIGTERM
# keeps its memory alongside its replacement indefinitely, until the OOM killer
# resolves it. 60s is well beyond a normal in-flight drain while still capping
# the window at 2x one worker.
export GRANIAN_WORKERS_KILL_TIMEOUT=${GRANIAN_WORKERS_KILL_TIMEOUT:-60}

# Run Granian

exec granian --interface asgi glitchtip.asgi:application --host $HOST --port $PORT --workers $WORKERS --log-level $G_LOG_LEVEL --no-ws "$@"

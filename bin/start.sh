#!/usr/bin/env sh
set -e

# GlitchTip runs as one process: a granian ASGI server that also hosts the
# task worker and the scheduler. There are no roles to dispatch on — scale by
# running more of this same process.
. ./bin/run-all-in-one.sh

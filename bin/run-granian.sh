#!/usr/bin/env sh
# Compatibility shim: ./bin/start.sh is the only serving entrypoint, and it
# runs granian. Kept so an existing deployment that names this script keeps
# working; it will be removed in a later release.
set -e

exec ./bin/start.sh "$@"

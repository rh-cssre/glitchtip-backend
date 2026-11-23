#!/usr/bin/env sh
# Compatibility shim: GlitchTip runs a single process, so serving is
# ./bin/start.sh. Kept so an existing deployment that names this script keeps
# working; it will be removed in a later release.
set -e

exec ./bin/start.sh "$@"

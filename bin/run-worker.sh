#!/usr/bin/env sh
# There is no dedicated worker any more: the process started by
# ./bin/start.sh serves HTTP and consumes the task queue. This script refuses
# to run rather than quietly turning a worker deployment into a web server.
set -e

echo "A dedicated worker is no longer supported. GlitchTip runs one process" >&2
echo "that serves HTTP and consumes the task queue: run ./bin/start.sh as a" >&2
echo "single deployment and remove this one." >&2
exit 1

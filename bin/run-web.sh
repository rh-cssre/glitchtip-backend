#!/usr/bin/env sh
export LOG_LEVEL=${LOG_LEVEL:-INFO}
export USE_ASYNC_SERVER=${USE_ASYNC_SERVER:-true}

exec bin/run-granian.sh
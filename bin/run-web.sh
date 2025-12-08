#!/usr/bin/env sh
if [ "$USE_GRANIAN" = "true" ]; then
    exec bin/run-granian.sh
elif [ "$USE_ASYNC_SERVER" = "true" ]; then
    # Run the command for asynchronous server
    exec bin/run-uvicorn.sh
else
    # Run the original command
    exec bin/run-uwsgi.sh
fi

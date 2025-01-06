#!/usr/bin/env sh
if [ "$USE_ASYNC_SERVER" = "true" ]; then
    # Run the command for asynchronous server
    bin/run-uvicorn.sh
else
    # Run the original command
    bin/run-uwsgi.sh
fi

"""
ASGI wrapper that starts memray tracking inside the granian worker process.

Usage: Set MEMRAY_ENABLED=1 and use this module as the ASGI entrypoint:
  granian --interface asgi benchmarks.memray_asgi_wrapper:application ...

This captures all allocations in the actual worker process, including
C-level allocations from psycopg, pydantic, orjson, etc.
"""

import atexit
import os

import memray

# Import the real application
from glitchtip.asgi import application as _real_application

_OUTPUT_DIR = "/code/benchmarks"
_tracker = None


def _start_tracking():
    global _tracker
    pid = os.getpid()
    path = os.path.join(_OUTPUT_DIR, f"memray_worker_{pid}.bin")
    print(f"[memray] Starting tracker in worker {pid} -> {path}", flush=True)
    _tracker = memray.Tracker(
        path,
        native_traces=False,
        follow_fork=True,
    )
    _tracker.__enter__()


def _stop_tracking():
    global _tracker
    if _tracker is not None:
        print(f"[memray] Stopping tracker in worker {os.getpid()}", flush=True)
        _tracker.__exit__(None, None, None)
        _tracker = None


# Start tracking immediately when this module is loaded (in the worker)
_start_tracking()
atexit.register(_stop_tracking)


async def application(scope, receive, send):
    """Proxy to the real ASGI application."""
    await _real_application(scope, receive, send)

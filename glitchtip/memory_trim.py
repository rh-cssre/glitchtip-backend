"""Periodic in-process memory trim for long-running ASGI processes.

Long-running processes accumulate freed-but-unreturned glibc heap pages
(malloc keeps freed chunks binned for reuse and only returns the top of the
heap automatically). Over days of steady ingest this adds tens of MB per
process that the OS counts against the pod. ``gc.collect()`` +
``malloc_trim(0)`` returns those pages.

The scheduled maintenance task already does this, but scheduled tasks run
once cluster-wide per interval — only the pod whose worker picks the task
up gets trimmed. This wrapper runs the same trim inside every ASGI process
on a conservative timer, which costs far less than the worker recycling
(``GRANIAN_WORKERS_MAX_RSS``) it helps avoid. Dedicated ``runworker``
processes don't go through ASGI and keep relying on the maintenance-task
trim.
"""

import asyncio
import ctypes
import gc
import logging
import random

logger = logging.getLogger(__name__)


def malloc_trim():
    """Ask glibc to return freed memory to the OS. No-op on other libcs."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class PeriodicMemoryTrim:
    """ASGI wrapper that periodically returns freed memory to the OS.

    The timer task starts lazily on the first ASGI call of any scope type
    (including lifespan), when an event loop is guaranteed to be running —
    this works under servers with or without lifespan support, and idle
    pods with an embedded worker still start trimming off their lifespan
    startup. Each server worker process wraps its own application instance,
    so each process gets its own timer.

    ``settings.GLITCHTIP_MALLOC_TRIM_INTERVAL`` (seconds) sets the mean
    interval; ``0`` disables the timer entirely. Each cycle is jittered
    ±50% so a fleet rolled out together doesn't pause in lockstep.
    """

    def __init__(self, app, interval: float | None = None):
        self.app = app
        if interval is None:
            from django.conf import settings

            interval = settings.GLITCHTIP_MALLOC_TRIM_INTERVAL
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def __call__(self, scope, receive, send):
        if self.interval > 0 and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="periodic-memory-trim"
            )
        if scope["type"] == "lifespan":
            receive = self._cancel_on_shutdown(receive)
        await self.app(scope, receive, send)

    def _cancel_on_shutdown(self, receive):
        """Stop the timer when the server begins lifespan shutdown, so loop
        teardown doesn't destroy a pending task (stderr noise on recycles)."""

        async def wrapped():
            message = await receive()
            if message["type"] == "lifespan.shutdown" and self._task is not None:
                self._task.cancel()
            return message

        return wrapped

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval * (0.5 + random.random()))
            try:
                # gc.collect() holds the GIL for its whole pass (~50-300 ms
                # on large heaps) — a thread wouldn't unblock the loop, so
                # run it inline. malloc_trim is a ctypes call that releases
                # the GIL, so that half does come off the loop.
                gc.collect()
                await asyncio.get_running_loop().run_in_executor(None, malloc_trim)
            except Exception:
                logger.exception("Periodic memory trim failed")

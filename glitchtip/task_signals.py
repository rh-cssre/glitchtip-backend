"""Worker-side cleanup that mirrors HTTP middleware.

django-async-backend keeps its own per-asyncio-Task connection store
(``async_connections``). The HTTP path calls ``close_all()`` in
``django_async_backend.middleware.close_async_connections``; the task
worker has no equivalent middleware, so async DB connections opened by a
task leak when the task's coroutine ends.

We re-close on both ``task_finished`` and ``task_failure``: vtasks fires
exactly one of the two per task (including on cancellation), and both
receivers run inside the same asyncio.Task that executed the user task,
which is the Task that owns the connection wrappers.

When ``USE_ASYNC_BACKEND`` is off, the sync DB connection layer cleans
itself up between tasks the normal Django way and no signal handler is
needed.
"""

import asyncio
import logging

from django_vtasks.signals import task_failure, task_finished

from glitchtip.async_compat import USE_ASYNC_BACKEND, async_connections

logger = logging.getLogger(__name__)


async def _close_async_connections(**_kwargs):
    try:
        await asyncio.shield(async_connections.close_all())
    except Exception:
        logger.exception("Failed to close async DB connections after task")


if USE_ASYNC_BACKEND:
    task_finished.connect(_close_async_connections, weak=False)
    task_failure.connect(_close_async_connections, weak=False)

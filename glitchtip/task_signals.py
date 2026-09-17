"""Worker-side cleanup that mirrors the HTTP request signals.

django-async-backend keeps its own per-asyncio-Task connection store
(``async_connections``). The HTTP path recycles it from the
``request_started`` / ``request_finished`` receivers ``glitchtip.apps``
connects; the task worker emits neither signal, so async DB connections
opened by a task leak when the task's coroutine ends.

We re-recycle on both ``task_finished`` and ``task_failure``: vtasks fires
exactly one of the two per task (including on cancellation), and both
receivers run inside the same asyncio.Task that executed the user task,
which is the Task that owns the connection wrappers.
"""

import logging

from django_vtasks.signals import task_failure, task_finished

from .async_db import recycle_async_connections

logger = logging.getLogger(__name__)


async def _recycle_async_connections(**_kwargs):
    # Awaited directly, not shielded: recycle_async_connections() shields
    # its own close and has to run in this task's context to unclaim the
    # aliases this task claimed.
    try:
        await recycle_async_connections()
    except Exception:
        logger.exception("Failed to recycle async DB connections after task")


task_finished.connect(_recycle_async_connections, weak=False)
task_failure.connect(_recycle_async_connections, weak=False)

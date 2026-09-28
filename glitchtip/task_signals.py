"""Worker-side cleanup that mirrors the HTTP request middleware.

django-async-backend keeps its own per-asyncio-Task connection store
(``async_connections``). The HTTP path recycles it in
``glitchtip.async_db.recycle_async_connections_middleware``; the task
worker runs no middleware, so async DB connections opened by a task leak
when the task's coroutine ends — and the alias stays claimed by it.

We recycle on both ``task_finished`` and ``task_failure``: vtasks fires
exactly one of the two per task, including on cancellation.

``Signal.asend`` runs an async receiver in a child task rather than the
one that executed the user task, so the fresh wrappers this plants would
be invisible here were it not for asgiref's ``_restore_context``, which
``asend`` calls on the way out and which copies them back. That is also
why the receiver awaits the recycle directly: a bare ``asyncio.shield()``
or ``create_task()`` restores nothing.
"""

import logging

from django_vtasks.signals import task_failure, task_finished

from .async_db import recycle_async_connections

logger = logging.getLogger(__name__)


async def _recycle_async_connections(**_kwargs):
    try:
        await recycle_async_connections()
    except Exception:
        logger.exception("Failed to recycle async DB connections after task")


task_finished.connect(_recycle_async_connections, weak=False)
task_failure.connect(_recycle_async_connections, weak=False)

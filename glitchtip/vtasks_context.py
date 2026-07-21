"""Per-task Sentry scope isolation for the vtasks worker.

Wired via ``VTASKS_TASK_CONTEXT`` (django-vtasks >= 3.1; older versions
ignore the setting). Embedded (ASGI) workers share the event loop — and
therefore the ambient Sentry isolation scope — with HTTP requests and the
scheduler, so without this every task's SQL breadcrumbs bleed into every
capture in the process, and task-side captures carry other tasks' and
requests' crumbs. Fork-and-clear per task gives each execution its own
breadcrumb trail, plus tags/context identifying the task.
"""

from contextlib import contextmanager

import sentry_sdk


@contextmanager
def sentry_task_scope(*, name, queue, task_id=None, task_ids=None, **_):
    with sentry_sdk.isolation_scope() as scope:
        scope.clear()
        scope.set_tag("vtasks.task", name)
        scope.set_tag("vtasks.queue", queue)
        scope.set_context(
            "vtasks",
            {
                "task_id": task_id,
                "task_ids": task_ids,
                "name": name,
                "queue": queue,
            },
        )
        yield

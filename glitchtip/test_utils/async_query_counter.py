"""Sync-callable counter for queries that run on async-backend cursors.

Django's ``assertNumQueries`` watches ``connections[alias]``, which is
distinct from ``async_connections[alias]`` — the two have separate cursor
classes, separate ``queries_log`` deques, and separate per-task wrappers.
So once the ingest hot path moved to ``async_connections``, the existing
assertions silently stopped seeing the queries they were meant to count.

``AsyncCaptureQueriesContext`` from django-async-backend is the natural
replacement, but two things block adopting it today:

1. The hot-path API tests POST through the Django test client. Django
   bridges its sync-only middleware (e.g. ``DecompressBodyMiddleware``,
   ``AuthenticationMiddleware``) with ``sync_to_async``, which dispatches
   to a worker thread. ``async_connections`` is ``thread_critical=True``,
   so the bridge thread gets its own wrapper instance — distinct
   ``queries_log``, distinct ``force_debug_cursor`` flag. The test
   thread's ``AsyncCaptureQueriesContext`` ends up watching the wrong
   wrapper and sees almost nothing.
2. Even on direct-ingest paths (``await process_issue_events(...)``)
   where there is no middleware bridge, ``queries_log`` captures
   ``BEGIN``/``COMMIT`` statements that the cursor-level patch here
   does not. Switching the counter would shift every test's expected
   number, producing churn that doesn't reflect a real change.

Patching ``_CursorTarget.execute`` / ``executemany`` at the class
level sidesteps the per-wrapper storage entirely. The patch is shared
by every wrapper in every thread, so the counter sees queries no matter
where they ran. This intentionally counts only queries that go through
async cursors; sync queries (test ``setUp`` fixtures, middleware, ORM
calls that haven't been ported yet) are invisible.

TODO: replace with ``AsyncCaptureQueriesContext`` once every entry in
``MIDDLEWARE`` is ``async_capable``. With the chain fully async, the
view runs in the test's task, the wrapper is shared, and the upstream
context manager becomes viable. That migration also needs a recalibration
pass on the expected query counts to absorb the BEGIN/COMMIT delta.
"""

from glitchtip.async_compat import USE_ASYNC_BACKEND

if USE_ASYNC_BACKEND:
    from django_async_backend.db.backends.utils import (
        AsyncCursorWrapper as _CursorTarget,
    )
else:
    # When async-backend is off, the same hot-path queries pass through
    # ``_SyncCursorProxy.execute`` / ``executemany`` (via
    # ``sync_to_async``). Patching that class at the class level gives
    # the counter the same coverage it had against ``AsyncCursorWrapper``.
    from glitchtip.async_compat import (
        _SyncCursorProxy as _CursorTarget,  # type: ignore[attr-defined]
    )


class AsyncQueryCounter:
    """Sync context manager that counts async-cursor executions.

    Usage::

        with AsyncQueryCounter() as counter:
            self.client.post(url, payload)
            task_backends["default"].flush_batches()
        self.assertEqual(len(counter), 8)
    """

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self) -> "AsyncQueryCounter":
        self._orig_execute = _CursorTarget.execute
        self._orig_executemany = _CursorTarget.executemany
        counter = self
        orig_execute = self._orig_execute
        orig_executemany = self._orig_executemany

        async def execute(cursor_self, sql, params=None):
            counter.count += 1
            return await orig_execute(cursor_self, sql, params)

        async def executemany(cursor_self, sql, param_list):
            counter.count += 1
            return await orig_executemany(cursor_self, sql, param_list)

        _CursorTarget.execute = execute
        _CursorTarget.executemany = executemany
        return self

    def __exit__(self, *_exc_info) -> None:
        _CursorTarget.execute = self._orig_execute
        _CursorTarget.executemany = self._orig_executemany

    def __len__(self) -> int:
        return self.count

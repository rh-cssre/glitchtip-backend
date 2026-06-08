"""Optional django-async-backend indirection.

When ``USE_ASYNC_BACKEND=True`` (opt-in), the symbols below are the real
async-backend primitives: native async cursors, ``async_atomic``,
``AsyncQuerySet``. When the flag is off (the default), this module
exposes thin ``sync_to_async`` shims over Django's stock sync ORM and
psycopg cursor.

The flag is read from the environment directly so this module has no
import-time dependency on ``django.conf.settings`` — it is imported by
``apps/shared/async_db.py`` which is in turn imported very early.

To remove this layer later: delete this file, set the engine to
``django_async_backend.db.backends.postgresql`` unconditionally, and
change the few call sites back to ``from django_async_backend.X import Y``.
"""

import os
from contextlib import asynccontextmanager
from typing import Any

from asgiref.sync import sync_to_async


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


USE_ASYNC_BACKEND: bool = _env_bool("USE_ASYNC_BACKEND", False)


if USE_ASYNC_BACKEND:
    from django_async_backend.db import async_connections
    from django_async_backend.db.models.query import QuerySet as AsyncQuerySet
    from django_async_backend.db.transaction import async_atomic

    __all__ = [
        "USE_ASYNC_BACKEND",
        "async_connections",
        "AsyncQuerySet",
        "async_atomic",
    ]

else:
    from django.db import connections, transaction
    from psycopg import ClientCursor

    class _SyncCursorProxy:
        """Async-shaped proxy around a Django sync cursor.

        Every method hops to the sync executor via ``sync_to_async``
        (``thread_sensitive=True`` — the asyncio default), which keeps all
        DB I/O for a given task on a single thread so Django's
        thread-local connection state and any open transaction remain
        consistent across awaits.
        """

        def __init__(self, cursor) -> None:
            self._cursor = cursor

        async def execute(self, sql: str, params: Any = None):
            return await sync_to_async(self._cursor.execute)(sql, params)

        async def executemany(self, sql: str, param_list):
            return await sync_to_async(self._cursor.executemany)(sql, param_list)

        async def fetchall(self):
            return await sync_to_async(self._cursor.fetchall)()

        async def fetchone(self):
            return await sync_to_async(self._cursor.fetchone)()

        async def fetchmany(self, size: int | None = None):
            if size is None:
                return await sync_to_async(self._cursor.fetchmany)()
            return await sync_to_async(self._cursor.fetchmany)(size)

        @property
        def description(self):
            return self._cursor.description

        @property
        def rowcount(self):
            return self._cursor.rowcount

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            await sync_to_async(self._cursor.close)()
            return None

    class _SyncOps:
        """Minimal stand-in for ``conn.ops.compose_sql``.

        Mogrifies via a temporary psycopg ``ClientCursor`` — psycopg3's
        server-side cursors don't expose mogrify, but ClientCursor does
        and operates on the same raw connection.
        """

        def __init__(self, alias: str) -> None:
            self._alias = alias

        async def compose_sql(self, template: str, params):
            def _do():
                conn = connections[self._alias]
                conn.ensure_connection()
                cc = ClientCursor(conn.connection)
                try:
                    return cc.mogrify(template, params)
                finally:
                    cc.close()

            return await sync_to_async(_do)()

    class _SyncConnectionProxy:
        def __init__(self, alias: str) -> None:
            self._alias = alias
            self.ops = _SyncOps(alias)

        async def cursor(self):
            # Resolve ``connections[alias]`` *inside* the sync executor —
            # ``connections`` is a per-thread handler, so a wrapper looked
            # up on the event-loop thread can't be used from the worker
            # thread (Django raises ``DatabaseError`` on thread sharing).
            alias = self._alias

            def _open():
                return connections[alias].cursor()

            sync_cursor = await sync_to_async(_open)()
            return _SyncCursorProxy(sync_cursor)

        async def close(self):
            alias = self._alias

            def _close():
                connections[alias].close()

            await sync_to_async(_close)()

    class _SyncConnectionsProxy:
        """Stand-in for ``django_async_backend.db.async_connections``."""

        # ``async-backend`` keeps a per-asyncio-Task store at ``_connections``;
        # the test helper in apps/event_ingest/tests/utils.py probes it with
        # ``hasattr(async_connections._connections, alias)`` to decide whether
        # to close. With the sync shim there is nothing task-local to close,
        # so an empty sentinel makes every ``hasattr`` lookup return False.
        _connections = object()

        def __getitem__(self, alias: str) -> _SyncConnectionProxy:
            return _SyncConnectionProxy(alias)

        def __contains__(self, alias: str) -> bool:
            from django.conf import settings as dj_settings

            return alias in dj_settings.DATABASES

        async def close_all(self):
            def _do():
                for alias in list(connections):
                    connections[alias].close()

            await sync_to_async(_do)()

        @property
        def settings(self):
            from django.conf import settings as dj_settings

            return dj_settings.DATABASES

    async_connections = _SyncConnectionsProxy()

    @asynccontextmanager
    async def async_atomic(
        using: str | None = None, savepoint: bool = True, durable: bool = False
    ):
        sync_cm = transaction.atomic(using=using, savepoint=savepoint, durable=durable)
        await sync_to_async(sync_cm.__enter__)()
        try:
            yield
        except BaseException as exc:
            await sync_to_async(sync_cm.__exit__)(type(exc), exc, exc.__traceback__)
            raise
        else:
            await sync_to_async(sync_cm.__exit__)(None, None, None)

    def AsyncQuerySet(*, model, using: str | None = None):
        """Return a stock Django QuerySet bound to ``using``.

        Django's QuerySet already exposes ``aget``/``aiterator``/``__aiter__``
        as ``sync_to_async`` wrappers, so call sites that only use those
        async methods don't need to know which mode is active.
        """
        qs = model._default_manager.get_queryset()
        if using is not None:
            qs = qs.using(using)
        return qs

    __all__ = [
        "USE_ASYNC_BACKEND",
        "async_connections",
        "AsyncQuerySet",
        "async_atomic",
    ]

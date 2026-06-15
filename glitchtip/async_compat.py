"""Optional django-async-backend indirection.

When ``USE_ASYNC_BACKEND=True`` (opt-in), the symbols below are the real
async-backend primitives: native async cursors, ``async_atomic``,
``AsyncQuerySet``. When the flag is off (the default), this module
exposes ``sync_to_async`` shims over Django's stock sync ORM:
``async_atomic`` wraps ``transaction.atomic`` and ``AsyncQuerySet``
returns a stock QuerySet.

Note there is intentionally no async-cursor shim here. Wrapping a sync
cursor method-by-method would make one logical query cost several
serialized ``thread_sensitive`` hops (open · execute · fetch · close);
instead :mod:`apps.shared.async_db` runs each query's full block in a
single ``sync_to_async`` against Django's stock ``connections``, the way
a vanilla Django view would. ``async_connections`` here only needs to
cover the connection lifecycle that the test teardown drives.

The flag is read from the environment directly so this module has no
import-time dependency on ``django.conf.settings`` — it is imported by
``apps/shared/async_db.py`` which is in turn imported very early.

To remove this layer later: delete this file, set the engine to
``django_async_backend.db.backends.postgresql`` unconditionally, and
change the few call sites back to ``from django_async_backend.X import Y``.
"""

import os
from contextlib import asynccontextmanager

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

    class _SyncConnectionProxy:
        """Stand-in for a single ``async_connections[alias]`` entry.

        The hot-path raw-SQL helpers no longer go through here — when the
        flag is off, :mod:`apps.shared.async_db` talks to Django's stock
        ``connections`` directly, running each query's full
        open/execute/fetch/close block in a *single* ``sync_to_async`` hop
        (the way a vanilla Django view would). What remains is the
        ``close()`` the per-task test teardown calls to release the
        connection on the same thread that opened it.
        """

        def __init__(self, alias: str) -> None:
            self._alias = alias

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

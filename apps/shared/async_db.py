"""Async raw-SQL helpers for the ingest hot paths (events, logs, spans).

Two implementations, picked once at import time by ``USE_ASYNC_BACKEND``:

* **Native** (flag on): django-async-backend's async cursor — real async
  I/O, zero thread hops.
* **Sync shim** (flag off, the default): each helper runs its whole
  ``open → execute → fetch → close`` block inside a *single*
  ``sync_to_async`` hop, exactly the way a hand-written Django view would
  wrap a synchronous cursor. This is deliberately coarse: the async
  cursor interface tempts you to wrap each *method* in its own
  ``sync_to_async`` (``fetchall`` alone would be four serialized hops
  through the one ``thread_sensitive`` executor thread — open, execute,
  fetch, close), which dwarfs the actual query work. One hop per logical
  query keeps the sync path honest against the native one and, as a bonus,
  collapses the await boundaries where a sibling task could poison the
  shared thread-local connection.

The native path keeps the fine-grained ``await`` calls because there is no
thread hop to pay for — the awaits suspend on socket I/O, not on an
executor.
"""

from typing import Any

from glitchtip.async_compat import USE_ASYNC_BACKEND

if USE_ASYNC_BACKEND:
    from glitchtip.async_compat import async_connections

    async def fetchall(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> tuple[list[str], list[tuple]]:
        """Execute ``sql`` with ``params`` and return ``(columns, rows)``."""
        async with await async_connections[db_alias].cursor() as cursor:
            await cursor.execute(sql, params)
            columns = [c[0] for c in cursor.description]
            rows = await cursor.fetchall()
            return columns, rows

    async def fetchone(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> tuple | None:
        """Execute ``sql`` with ``params`` and return a single row (or None)."""
        async with await async_connections[db_alias].cursor() as cursor:
            await cursor.execute(sql, params)
            return await cursor.fetchone()

    async def execute(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> int:
        """Execute ``sql`` with ``params`` and return ``cursor.rowcount``.

        For UPDATE/INSERT/DELETE that doesn't need rows back. Use
        :func:`fetchall` for SELECT.
        """
        async with await async_connections[db_alias].cursor() as cursor:
            await cursor.execute(sql, params)
            return cursor.rowcount

    async def fetchall_mogrified_values(
        sql_template: str,
        values_fragment: str,
        value_params: list[tuple],
        db_alias: str = "default",
    ) -> tuple[list[str], list[tuple]]:
        """Mogrify ``value_params`` into ``values_fragment`` (one row each),
        substitute the joined literals into ``sql_template`` where
        ``{values}`` appears, then execute.

        Mirrors the ``cursor.mogrify("(%s,%s::uuid)", pair)`` pattern we
        use in :func:`~apps.event_ingest.process_event._fetch_issue_hashes_raw`.
        """
        conn = async_connections[db_alias]
        parts: list[str] = []
        for row in value_params:
            # compose_sql may return bytes (psycopg2-style) or str
            # (psycopg3 ClientCursor). Normalise before joining.
            part = await conn.ops.compose_sql(values_fragment, row)
            if isinstance(part, (bytes, bytearray)):
                part = part.decode()
            parts.append(part)
        final = sql_template.format(values=",".join(parts))
        async with await conn.cursor() as cursor:
            await cursor.execute(final)
            columns = [c[0] for c in cursor.description]
            rows = await cursor.fetchall()
            return columns, rows

    async def execute_mogrified_values(
        sql_template: str,
        values_fragment: str,
        value_params: list[tuple],
        db_alias: str = "default",
    ) -> int:
        """Like :func:`fetchall_mogrified_values` but for UPDATE/INSERT —
        discards any result rows and returns ``cursor.rowcount``."""
        conn = async_connections[db_alias]
        parts: list[str] = []
        for row in value_params:
            part = await conn.ops.compose_sql(values_fragment, row)
            if isinstance(part, (bytes, bytearray)):
                part = part.decode()
            parts.append(part)
        final = sql_template.format(values=",".join(parts))
        async with await conn.cursor() as cursor:
            await cursor.execute(final)
            return cursor.rowcount

else:
    from asgiref.sync import sync_to_async
    from django.db import connections
    from psycopg import ClientCursor

    def _fetchall_sync(
        sql: str, params: Any | None, db_alias: str
    ) -> tuple[list[str], list[tuple]]:
        with connections[db_alias].cursor() as cursor:
            cursor.execute(sql, params)
            columns = [c[0] for c in cursor.description]
            return columns, cursor.fetchall()

    async def fetchall(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> tuple[list[str], list[tuple]]:
        """Execute ``sql`` with ``params`` and return ``(columns, rows)``."""
        return await sync_to_async(_fetchall_sync)(sql, params, db_alias)

    def _fetchone_sync(sql: str, params: Any | None, db_alias: str) -> tuple | None:
        with connections[db_alias].cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()

    async def fetchone(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> tuple | None:
        """Execute ``sql`` with ``params`` and return a single row (or None)."""
        return await sync_to_async(_fetchone_sync)(sql, params, db_alias)

    def _execute_sync(sql: str, params: Any | None, db_alias: str) -> int:
        with connections[db_alias].cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    async def execute(
        sql: str,
        params: Any | None = None,
        db_alias: str = "default",
    ) -> int:
        """Execute ``sql`` with ``params`` and return ``cursor.rowcount``.

        For UPDATE/INSERT/DELETE that doesn't need rows back. Use
        :func:`fetchall` for SELECT.
        """
        return await sync_to_async(_execute_sync)(sql, params, db_alias)

    def _mogrify_run_sync(
        sql_template: str,
        values_fragment: str,
        value_params: list[tuple],
        db_alias: str,
        fetch: bool,
    ):
        conn = connections[db_alias]
        conn.ensure_connection()
        # psycopg3 server-side cursors don't expose mogrify; ClientCursor
        # does and operates on the same raw connection. One ClientCursor
        # mogrifies every row — no per-row hop, since the whole loop runs
        # inside this single executor call.
        cc = ClientCursor(conn.connection)
        try:
            parts = [cc.mogrify(values_fragment, row) for row in value_params]
        finally:
            cc.close()
        final = sql_template.format(values=",".join(parts))
        with conn.cursor() as cursor:
            cursor.execute(final)
            if fetch:
                columns = [c[0] for c in cursor.description]
                return columns, cursor.fetchall()
            return cursor.rowcount

    async def fetchall_mogrified_values(
        sql_template: str,
        values_fragment: str,
        value_params: list[tuple],
        db_alias: str = "default",
    ) -> tuple[list[str], list[tuple]]:
        """Mogrify ``value_params`` into ``values_fragment`` (one row each),
        substitute the joined literals into ``sql_template`` where
        ``{values}`` appears, then execute.

        Mirrors the ``cursor.mogrify("(%s,%s::uuid)", pair)`` pattern we
        use in :func:`~apps.event_ingest.process_event._fetch_issue_hashes_raw`.
        """
        return await sync_to_async(_mogrify_run_sync)(
            sql_template, values_fragment, value_params, db_alias, True
        )

    async def execute_mogrified_values(
        sql_template: str,
        values_fragment: str,
        value_params: list[tuple],
        db_alias: str = "default",
    ) -> int:
        """Like :func:`fetchall_mogrified_values` but for UPDATE/INSERT —
        discards any result rows and returns ``cursor.rowcount``."""
        return await sync_to_async(_mogrify_run_sync)(
            sql_template, values_fragment, value_params, db_alias, False
        )


async def execute_unnest(
    sql: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> int:
    """Transpose row-major ``value_params`` to per-column arrays and execute.

    ``sql`` is expected to call ``unnest(%s::T[], %s::T[], ...)`` with one
    ``%s`` per column. This avoids the per-row mogrify round-trip and the
    65535 bind-parameter cap that ``execute_mogrified_values`` hits with
    wide schemas, and gives Postgres a single statement shape for the
    plan cache regardless of batch size.
    """
    if not value_params:
        return 0
    columns = [list(c) for c in zip(*value_params)]
    return await execute(sql, columns, db_alias=db_alias)


async def fetchall_unnest(
    sql: str,
    value_params: list[tuple],
    db_alias: str = "default",
) -> tuple[list[str], list[tuple]]:
    """Like :func:`execute_unnest` but returns ``(columns, rows)``."""
    if not value_params:
        return [], []
    columns = [list(c) for c in zip(*value_params)]
    return await fetchall(sql, columns, db_alias=db_alias)

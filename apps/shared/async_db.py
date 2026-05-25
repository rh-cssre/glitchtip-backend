"""Async raw-SQL helpers for the ingest hot paths (events, logs, spans).

Uses django-async-backend's native async cursor so the event loop keeps
running while Postgres does its work.
"""

from __future__ import annotations

from typing import Any

from django_async_backend.db import async_connections


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

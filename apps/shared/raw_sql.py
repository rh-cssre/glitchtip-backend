"""Async raw-SQL helpers for the ingest hot paths (events, logs, spans).

Uses django-async-backend's native async cursor via ``async_connections``.
"""

from collections.abc import Iterable
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
        parts.append(await conn.ops.compose_sql(values_fragment, row))
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
        parts.append(await conn.ops.compose_sql(values_fragment, row))
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


async def copy_rows(
    table: str,
    columns: list[str],
    rows: Iterable[tuple],
    db_alias: str = "default",
) -> int:
    """COPY ``rows`` into ``table`` (text format, streamed row-by-row).

    Unlike a composed INSERT statement — which stages the entire batch in
    the connection's libpq output buffer and permanently grows it to the
    largest batch ever sent — COPY streams in small chunks, so connection
    memory stays bounded regardless of batch size. It also skips composing
    the batch into one SQL string in Python.

    COPY cannot express ON CONFLICT: a conflicting row aborts the whole
    batch (raised as ``django.db.IntegrityError``). Callers that need
    conflict tolerance must catch it, confirm the conflict with
    :func:`is_unique_violation`, and fall back to their INSERT path.
    Must run in autocommit (the async-backend default) — inside a
    transaction a failed COPY leaves it aborted, so a fallback INSERT
    would fail too.

    The gt_rust cursor exposes the psycopg-shaped ``copy()`` context
    manager; ``write_rows()`` encodes the whole batch in Rust (C-API field
    access, one Python↔Rust crossing per ~64 KiB chunk) — measurably less
    CPU per batch than a per-row loop.
    """
    quoted = [table, *columns]
    cols = ", ".join('"' + c.replace('"', '""') + '"' for c in quoted[1:])
    stmt = 'COPY "' + table.replace('"', '""') + f'" ({cols}) FROM STDIN'
    count = 0
    async with await async_connections[db_alias].cursor() as cursor:
        # Go straight to the driver cursor: the wrapper's attribute proxy
        # doesn't wrap exceptions (handled here so callers see Django's
        # IntegrityError), and the psycopg debug wrapper's ``copy``
        # override is an async generator built for COPY TO iteration — it
        # can't serve as the COPY FROM context manager.
        with cursor.db.wrap_database_errors:
            async with cursor.cursor.copy(stmt) as copy:
                if not isinstance(rows, (list, tuple)):
                    rows = list(rows)
                await copy.write_rows(rows)
                count = len(rows)
    return count


UNIQUE_VIOLATION = "23505"


def is_unique_violation(exc: Exception) -> bool:
    """True when a Django ``IntegrityError`` wraps a unique violation.

    ``IntegrityError`` covers all of SQLSTATE class 23 — unique violation,
    but also e.g. a missing partition (23514) or a foreign-key violation —
    and only the unique violation is worth retrying through a
    conflict-tolerant INSERT; the rest would fail identically. The gt_rust
    driver exposes the psycopg-shaped ``sqlstate`` attribute on the
    underlying DB-API exception (Django chains it as ``__cause__``); a
    ``sqlstate`` of None means a client-side error — known non-conflict,
    fail closed.
    """
    cause = exc.__cause__
    if cause is None:
        return False
    return getattr(cause, "sqlstate", None) == UNIQUE_VIOLATION

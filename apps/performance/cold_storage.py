"""
Cold storage query layer for Performance Monitoring V2.

Queries span Parquet files produced by the promotion job.
Handles two file layouts:
- Chunk files: org_{id}/{date}/chunk_*.parquet (pre-compaction)
- Compacted files: org_{id}/{date}.parquet (post-compaction)
"""

import logging
from datetime import datetime

from glitchtip.cold_storage import (
    close_duckdb_read_connection,
    duckdb_quote_path,
    enumerate_org_parquet_files,
    get_cold_storage_backend,
    get_duckdb_parquet_path,
    get_duckdb_read_connection,
    is_duckdb_available,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "performance_spans"

SPAN_PARQUET_COLUMN_TYPES = {
    "organization_id": "INTEGER",
    "project_id": "INTEGER",
    "transaction_name": "VARCHAR",
    "span_id": "VARCHAR",
    "transaction_id": "VARCHAR",
    "op": "VARCHAR",
    "description": "VARCHAR",
    "duration": "DOUBLE",
    "timestamp": "TIMESTAMP WITH TIME ZONE",
}


def _execute_resilient_query(storage, duckdb_paths, sql_builder, params):
    """
    Execute a DuckDB query across multiple parquet files.

    Fast path: query all files at once. If any file is corrupt,
    validates files individually and retries with only valid ones.
    """
    paths_list = ", ".join(f"'{duckdb_quote_path(p)}'" for p in duckdb_paths)
    duck_conn = get_duckdb_read_connection(storage)
    try:
        return duck_conn.execute(sql_builder(paths_list), params).fetchall()
    except Exception:
        close_duckdb_read_connection()
        logger.warning(
            "Multi-file parquet query failed, validating individual files",
            exc_info=True,
        )

    # Identify valid files
    valid = []
    for p in duckdb_paths:
        conn = get_duckdb_read_connection(storage)
        try:
            conn.execute(
                f"SELECT 1 FROM read_parquet('{duckdb_quote_path(p)}') LIMIT 0"
            )
            valid.append(p)
        except Exception:
            close_duckdb_read_connection()
            logger.error("Corrupt parquet file skipped: %s", p, exc_info=True)

    if not valid:
        return []

    paths_list = ", ".join(f"'{duckdb_quote_path(p)}'" for p in valid)
    conn = get_duckdb_read_connection(storage)
    try:
        return conn.execute(sql_builder(paths_list), params).fetchall()
    except Exception:
        close_duckdb_read_connection()
        logger.error("Query failed even after file validation", exc_info=True)
        return []


def _get_duckdb_paths(storage, org_id, start_dt, end_dt):
    """Get DuckDB-readable paths for an org's parquet files in a date range."""
    rel_paths = enumerate_org_parquet_files(
        storage, TABLE_NAME, org_id, start_dt, end_dt
    )
    return [get_duckdb_parquet_path(storage, p) for p in rel_paths]


def query_span_groups_for_transaction(
    org_id: int,
    transaction_name: str,
    start_dt: datetime,
    end_dt: datetime,
    limit: int = 50,
) -> list[dict]:
    """
    Query span groups for a specific transaction.

    Args:
        transaction_name: The resolved transaction name (caller must look up the
            TransactionGroup and pass group.transaction).

    Returns list of {op, description, count, avg_duration, p95_duration, total_time}
    grouped by (op, description), ordered by total_time DESC.
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    duckdb_paths = _get_duckdb_paths(storage, org_id, start_dt, end_dt)
    if not duckdb_paths:
        return []

    params = [transaction_name, start_dt, end_dt, limit]

    def sql_builder(paths_list):
        return f"""
            SELECT
                op,
                description,
                COUNT(*) as count,
                AVG(duration) as avg_duration,
                PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration) as p95_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE transaction_name = $1
              AND timestamp >= $2
              AND timestamp < $3
            GROUP BY op, description
            ORDER BY total_time DESC
            LIMIT $4
        """

    rows = _execute_resilient_query(storage, duckdb_paths, sql_builder, params)

    return [
        {
            "op": row[0],
            "description": row[1] or "",
            "count": row[2],
            "avg_duration": row[3] or 0,
            "p95_duration": row[4] or 0,
            "total_time": row[5] or 0,
        }
        for row in rows
    ]


def query_n_plus_one_patterns(
    org_id: int,
    project_ids: list[int] | None,
    start_dt: datetime,
    end_dt: datetime,
    op_filter: str | None = "db",
    threshold: float = 5.0,
    limit: int = 50,
) -> list[dict]:
    """
    Detect N+1 query patterns by finding span groups with high per-transaction
    repetition counts.

    Returns list of {transaction_name, op, description, total_spans,
    transaction_count, spans_per_txn, avg_duration, total_time}
    ordered by spans_per_txn DESC.
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    duckdb_paths = _get_duckdb_paths(storage, org_id, start_dt, end_dt)
    if not duckdb_paths:
        return []

    # Build optional WHERE clauses
    extra_where = ""
    params: list = [start_dt, end_dt]
    param_idx = 3

    if op_filter:
        extra_where += f" AND op LIKE ${param_idx}"
        params.append(f"{op_filter}%")
        param_idx += 1

    if project_ids:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(project_ids)))
        extra_where += f" AND project_id IN ({placeholders})"
        params.extend(project_ids)
        param_idx += len(project_ids)

    params.extend([threshold, limit])

    def sql_builder(paths_list):
        return f"""
            SELECT
                transaction_name,
                op,
                description,
                COUNT(*) as total_spans,
                COUNT(DISTINCT transaction_id) as transaction_count,
                ROUND(COUNT(*) * 1.0 / COUNT(DISTINCT transaction_id), 1) as spans_per_txn,
                AVG(duration) as avg_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE timestamp >= $1
              AND timestamp < $2
              {extra_where}
            GROUP BY transaction_name, op, description
            HAVING COUNT(*) * 1.0 / COUNT(DISTINCT transaction_id) > ${param_idx}
            ORDER BY spans_per_txn DESC
            LIMIT ${param_idx + 1}
        """

    rows = _execute_resilient_query(storage, duckdb_paths, sql_builder, params)

    return [
        {
            "transaction_name": row[0],
            "op": row[1],
            "description": row[2] or "",
            "total_spans": row[3],
            "transaction_count": row[4],
            "spans_per_txn": row[5],
            "avg_duration": row[6] or 0,
            "total_time": row[7] or 0,
        }
        for row in rows
    ]


def query_span_groups(
    org_id: int,
    project_ids: list[int] | None,
    start_dt: datetime,
    end_dt: datetime,
    op_filter: str | None = None,
    sort: str = "-total_time",
    limit: int = 50,
) -> list[dict]:
    """
    Query span groups across the organization.

    Args:
        op_filter: Optional op prefix filter (e.g. "db" for database spans).
        sort: Sort field, prefixed with - for descending.
            Supported: total_time, avg_duration, count.

    Returns list of {op, description, count, avg_duration, p95_duration, total_time}
    grouped by (op, description).
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    duckdb_paths = _get_duckdb_paths(storage, org_id, start_dt, end_dt)
    if not duckdb_paths:
        return []

    # Build optional WHERE clauses
    extra_where = ""
    params: list = [start_dt, end_dt]
    param_idx = 3

    if op_filter:
        extra_where += f" AND op LIKE ${param_idx}"
        params.append(f"{op_filter}%")
        param_idx += 1

    if project_ids:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(project_ids)))
        extra_where += f" AND project_id IN ({placeholders})"
        params.extend(project_ids)
        param_idx += len(project_ids)

    # Map sort parameter to SQL ORDER BY
    sort_field = sort.lstrip("-")
    sort_dir = "DESC" if sort.startswith("-") else "ASC"
    allowed_sorts = {"total_time", "avg_duration", "count"}
    if sort_field not in allowed_sorts:
        sort_field = "total_time"
        sort_dir = "DESC"

    params.append(limit)

    def sql_builder(paths_list):
        return f"""
            SELECT
                op,
                description,
                COUNT(*) as count,
                AVG(duration) as avg_duration,
                PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration) as p95_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE timestamp >= $1
              AND timestamp < $2
              {extra_where}
            GROUP BY op, description
            ORDER BY {sort_field} {sort_dir}
            LIMIT ${param_idx}
        """

    rows = _execute_resilient_query(storage, duckdb_paths, sql_builder, params)

    return [
        {
            "op": row[0],
            "description": row[1] or "",
            "count": row[2],
            "avg_duration": row[3] or 0,
            "p95_duration": row[4] or 0,
            "total_time": row[5] or 0,
        }
        for row in rows
    ]


def query_transaction_trend(
    org_id: int,
    transaction_name: str,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
) -> list[dict]:
    """
    Query daily performance trend for a specific transaction.

    Returns one row per day with:
    - date: day bucket
    - count: total spans (all child spans in matching transactions)
    - transaction_count: distinct transaction/request count (throughput)
    - avg_duration: average span duration in ms
    - total_time: sum of all span durations in ms
    """
    if not is_duckdb_available():
        return []

    storage = get_cold_storage_backend()
    if not storage:
        return []

    duckdb_paths = _get_duckdb_paths(storage, org_id, start_dt, end_dt)
    if not duckdb_paths:
        return []

    extra_where = ""
    params: list = [transaction_name, start_dt, end_dt]
    param_idx = 4

    if project_ids:
        placeholders = ", ".join(f"${param_idx + i}" for i in range(len(project_ids)))
        extra_where += f" AND project_id IN ({placeholders})"
        params.extend(project_ids)

    def sql_builder(paths_list):
        return f"""
            SELECT
                DATE_TRUNC('day', timestamp) as date,
                COUNT(*) as count,
                COUNT(DISTINCT transaction_id) as transaction_count,
                AVG(duration) as avg_duration,
                SUM(duration) as total_time
            FROM read_parquet([{paths_list}])
            WHERE transaction_name = $1
              AND timestamp >= $2
              AND timestamp < $3
              {extra_where}
            GROUP BY DATE_TRUNC('day', timestamp)
            ORDER BY date
        """

    rows = _execute_resilient_query(storage, duckdb_paths, sql_builder, params)

    return [
        {
            "date": row[0],
            "count": row[1],
            "transaction_count": row[2],
            "avg_duration": row[3] or 0,
            "total_time": row[4] or 0,
        }
        for row in rows
    ]

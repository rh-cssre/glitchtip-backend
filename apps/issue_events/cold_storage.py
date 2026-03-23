"""
Cold storage utilities for archiving issue event partitions to Parquet via standalone DuckDB.

Thin wrapper around glitchtip.cold_storage with issue-event-specific configuration.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from glitchtip.cold_storage import (
    archive_and_swap_partition as _archive_and_swap_partition,
)
from glitchtip.cold_storage import (
    archive_partition_per_org as _archive_partition_per_org,
)
from glitchtip.cold_storage import (
    close_duckdb_read_connection,
    duckdb_quote_path,
    get_cold_storage_backend,
    get_duckdb_parquet_path,
    get_duckdb_read_connection,
    get_org_cold_storage_path,  # noqa: F401 — re-exported for tests
    get_parquet_paths_for_date,
    is_duckdb_available,
    is_missing_file_error,
    parse_json_field,
    parse_json_list_field,
)
from glitchtip.partition_manager import UUID7Helper

logger = logging.getLogger(__name__)

__all__ = [
    "ISSUE_EVENT_EXPORT_COLUMN_TYPES",
    "ISSUE_EVENT_SELECT_SQL",
    "IssueEventRow",
    "archive_and_swap_partition",
    "archive_partition_per_org",
    "get_event_from_cold",
    "query_cold_events",
]

TABLE_NAME = "issue_events_issueevent"

# DuckDB column types for the issue_events_issueevent export schema.
ISSUE_EVENT_EXPORT_COLUMN_TYPES = {
    "id": "VARCHAR",
    "event_id": "VARCHAR",
    "timestamp": "TIMESTAMP",
    "issue_id": "BIGINT",
    "organization_id": "BIGINT",
    "release_id": "BIGINT",
    "type": "SMALLINT",
    "level": "SMALLINT",
    "title": "VARCHAR",
    "transaction": "VARCHAR",
    "data": "VARCHAR",
    "tags": "VARCHAR",
    "hashes": "VARCHAR",
}

ISSUE_EVENT_SELECT_SQL = """
    SELECT id::text, event_id::text, timestamp, issue_id, organization_id, release_id,
           type, level, title, transaction, data::text, tags::text,
           array_to_json(hashes)::text
    FROM {partition_name}
    WHERE organization_id = %s
"""


@dataclass
class IssueEventRow:
    """Row from cold storage query, compatible with IssueEvent schema resolvers."""

    id: UUID
    event_id: UUID | None
    timestamp: datetime
    issue_id: int
    organization_id: int
    release_id: int | None
    type: int
    level: int
    title: str
    transaction: str
    data: dict
    tags: dict
    hashes: list[str]

    @property
    def eventID(self):
        return (self.event_id or self.id).hex

    @property
    def received(self):
        return UUID7Helper.extract_datetime(self.id)

    @property
    def message(self):
        return self.data.get("message", self.title)

    @property
    def metadata(self):
        return self.data.get("metadata", {"title": self.title})

    @property
    def platform(self):
        return self.data.get("platform")

    def get_type_display(self):
        from .constants import IssueEventType

        try:
            return IssueEventType(self.type).label
        except ValueError:
            return "default"

    def get_level_display(self):
        from .constants import LogLevel

        try:
            return LogLevel(self.level).label
        except ValueError:
            return "error"


def _row_to_issue_event(row: tuple) -> IssueEventRow:
    """Convert a database row (positional) to IssueEventRow."""
    return IssueEventRow(
        id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
        event_id=(
            row[1]
            if isinstance(row[1], UUID)
            else (UUID(str(row[1])) if row[1] else None)
        ),
        timestamp=row[2],
        issue_id=row[3],
        organization_id=row[4],
        release_id=row[5],
        type=row[6],
        level=row[7],
        title=row[8],
        transaction=row[9] or "",
        data=parse_json_field(row[10]),
        tags=parse_json_field(row[11]),
        hashes=parse_json_list_field(row[12]),
    )


DICTIONARY_COLUMNS: set[str] = set()


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
) -> list[tuple[int, str]]:
    """Archive an issue event partition to cold storage as per-org Parquet files."""
    return _archive_partition_per_org(
        partition_name,
        date_str,
        TABLE_NAME,
        ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        ISSUE_EVENT_SELECT_SQL,
        dictionary_columns=DICTIONARY_COLUMNS,
    )


def archive_and_swap_partition(
    partition_name: str,
) -> bool:
    """Full archival workflow for issue event partitions."""
    return _archive_and_swap_partition(
        partition_name,
        TABLE_NAME,
        ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        ISSUE_EVENT_SELECT_SQL,
        dictionary_columns=DICTIONARY_COLUMNS,
    )


def query_cold_events(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    issue_id: int | None = None,
    event_id: UUID | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[IssueEventRow]:
    """
    Query issue events from cold storage (per-org Parquet files via standalone DuckDB).

    Enumerates files individually so a corrupt file doesn't poison the
    entire query. DuckDB runs in-process — no PostgreSQL extension required.
    """
    if not is_duckdb_available():
        return []

    from glitchtip.cold_storage import query_cold_parquet_files

    # Build WHERE clause with DuckDB $N positional parameters
    where_parts = ["organization_id = $1"]
    params: list = [organization_id]

    # Time range via UUIDv7 bounds
    start_uuid, end_uuid = UUID7Helper.get_range_for_date(start_dt, end_dt)
    params.extend([str(start_uuid), str(end_uuid)])
    where_parts.append(f"id >= ${len(params) - 1}")
    where_parts.append(f"id < ${len(params)}")

    if issue_id is not None:
        params.append(issue_id)
        where_parts.append(f"issue_id = ${len(params)}")

    if event_id is not None:
        params.append(str(event_id))
        where_parts.append(f"event_id = ${len(params)}")

    if cursor_position:
        params.append(str(cursor_position))
        where_parts.append(f"id < ${len(params)}")

    where_sql = " AND ".join(where_parts)

    params.append(int(limit))
    limit_param = f"${len(params)}"

    select_columns = (
        "id, event_id, timestamp, issue_id, organization_id, release_id, "
        "type, level, title, transaction, data, tags, hashes"
    )

    rows = query_cold_parquet_files(
        organization_id=organization_id,
        table_name=TABLE_NAME,
        select_columns=select_columns,
        where_sql=where_sql,
        params=params,
        limit_param=limit_param,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    results = [_row_to_issue_event(row) for row in rows]
    results.sort(key=lambda r: r.id, reverse=True)
    return results[:limit]


def get_event_from_cold(
    organization_id: int,
    event_id: UUID,
    event_time: datetime,
) -> IssueEventRow | None:
    """
    Fetch a single issue event from cold storage by its UUIDv7 id.

    Uses the timestamp from the UUIDv7 to target the correct Parquet file.
    """
    if not is_duckdb_available():
        return None

    storage = get_cold_storage_backend()
    if not storage:
        return None

    date_str = event_time.strftime("%Y%m%d")
    paths = get_parquet_paths_for_date(storage, TABLE_NAME, organization_id, date_str)
    if not paths:
        return None

    duck_conn = get_duckdb_read_connection(storage)
    for relative_path in paths:
        parquet_path = get_duckdb_parquet_path(storage, relative_path)
        try:
            sql = f"""
                SELECT id, event_id, timestamp, issue_id, organization_id, release_id,
                       type, level, title, transaction, data, tags, hashes
                FROM read_parquet('{duckdb_quote_path(parquet_path)}')
                WHERE id = $1 AND organization_id = $2
                LIMIT 1;
            """
            result = duck_conn.execute(sql, [str(event_id), organization_id])
            row = result.fetchone()
            if row:
                return _row_to_issue_event(row)
        except Exception as e:
            close_duckdb_read_connection()
            if not is_missing_file_error(e):
                raise
            duck_conn = get_duckdb_read_connection(storage)

    return None

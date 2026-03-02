"""
Cold storage utilities for archiving log partitions to Parquet via standalone DuckDB.

This is a thin wrapper around glitchtip.cold_storage with logs-specific configuration.
"""

from glitchtip.cold_storage import (
    archive_and_swap_partition as _archive_and_swap_partition,
)
from glitchtip.cold_storage import (
    archive_partition_per_org as _archive_partition_per_org,
)

# DuckDB column types for the logs_logevent export schema.
# UUIDs and JSONB are exported as VARCHAR strings.
EXPORT_COLUMN_TYPES = {
    "id": "VARCHAR",
    "trace_id": "VARCHAR",
    "organization_id": "BIGINT",
    "project_id": "BIGINT",
    "span_id": "VARCHAR",
    "level": "SMALLINT",
    "severity_number": "SMALLINT",
    "body": "VARCHAR",
    "service": "VARCHAR",
    "environment": "VARCHAR",
    "host": "VARCHAR",
    "data": "VARCHAR",
}

LOGS_SELECT_SQL = """
    SELECT id::text, trace_id::text, organization_id, project_id, span_id::text,
           level, severity_number, body, service, environment, host, data::text
    FROM {partition_name}
    WHERE organization_id = %s
"""

TABLE_NAME = "logs_logevent"


def archive_partition_per_org(
    partition_name: str,
    date_str: str,
    table_name: str = TABLE_NAME,
) -> list[tuple[int, str]]:
    """Archive a log partition to cold storage as per-org Parquet files."""
    return _archive_partition_per_org(
        partition_name,
        date_str,
        table_name,
        EXPORT_COLUMN_TYPES,
        LOGS_SELECT_SQL,
    )


def archive_and_swap_partition(
    partition_name: str,
    table_name: str = TABLE_NAME,
) -> bool:
    """Full archival workflow for log partitions."""
    return _archive_and_swap_partition(
        partition_name, table_name, EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL
    )

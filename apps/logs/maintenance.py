"""
Log maintenance tasks for archiving and cleanup.

Called from glitchtip.tasks.perform_maintenance nightly.
"""

import logging

from django.conf import settings

from glitchtip.cold_storage import (
    archive_and_cleanup_partitions,
    detach_partition,
    drop_partition,
    get_partitions_older_than,
    is_duckdb_available,
)

from .cold_storage import DICTIONARY_COLUMNS, EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL

logger = logging.getLogger(__name__)


def cleanup_old_logs():
    """
    Archive old log partitions to cold storage and delete expired cold data.

    Settings:
        GLITCHTIP_LOG_HOT_DAYS: Days to keep in hot storage (default 7)
        GLITCHTIP_LOG_RETENTION_DAYS: Total retention including cold (default 90)
    """
    if not settings.GLITCHTIP_ENABLE_LOGS:
        return

    hot_days = settings.GLITCHTIP_LOG_HOT_DAYS
    retention_days = settings.GLITCHTIP_LOG_RETENTION_DAYS

    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    if is_duckdb_available():
        archive_and_cleanup_partitions(
            table_name="logs_logevent",
            hot_days=hot_days,
            column_types=EXPORT_COLUMN_TYPES,
            select_sql=LOGS_SELECT_SQL,
            retention_days=retention_days,
            db_alias=db_alias,
            dictionary_columns=DICTIONARY_COLUMNS,
        )
    else:
        # No cold storage available - delete partitions at total retention
        delete_old_hot_partitions(retention_days, db_alias=db_alias)


def delete_old_hot_partitions(days: int, db_alias: str | None = None):
    """Delete hot partitions older than `days` when cold storage unavailable."""
    partitions = get_partitions_older_than("logs_logevent", days, db_alias=db_alias)

    if not partitions:
        return

    logger.info(
        f"Deleting {len(partitions)} old log partitions (cold storage unavailable)"
    )

    for name, date in partitions:
        try:
            detach_partition(name, "logs_logevent", db_alias=db_alias)
            drop_partition(name, db_alias=db_alias)
            logger.info(f"Deleted log partition {name}")
        except Exception as e:
            logger.error(f"Error deleting log partition {name}: {e}")

"""
Log maintenance tasks for archiving and cleanup.

Called from glitchtip.tasks.perform_maintenance nightly.
"""

import logging

from django.conf import settings

from glitchtip.cold_storage import (
    archive_and_cleanup_partitions,
    get_partitions_older_than,
    is_duckdb_available,
)

from .cold_storage import EXPORT_COLUMN_TYPES, LOGS_SELECT_SQL

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

    if is_duckdb_available():
        archive_and_cleanup_partitions(
            table_name="logs_logevent",
            hot_days=hot_days,
            column_types=EXPORT_COLUMN_TYPES,
            select_sql=LOGS_SELECT_SQL,
            retention_days=retention_days,
        )
    else:
        # No cold storage available - delete partitions at total retention
        delete_old_hot_partitions(retention_days)


def delete_old_hot_partitions(days: int):
    """Delete hot partitions older than `days` when cold storage unavailable."""
    from django.db import connection

    partitions = get_partitions_older_than("logs_logevent", days)

    if not partitions:
        return

    logger.info(
        f"Deleting {len(partitions)} old log partitions (cold storage unavailable)"
    )

    for name, date in partitions:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE logs_logevent DETACH PARTITION {name};")
                cursor.execute(f"DROP TABLE IF EXISTS {name} CASCADE;")
            logger.info(f"Deleted log partition {name}")
        except Exception as e:
            logger.error(f"Error deleting log partition {name}: {e}")

import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from glitchtip.partition_manager import PartitionManager

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Create future partitions and cleanup old ones"

    def handle(self, *args, **options):
        manager = PartitionManager(db_connection=settings.MAINTENANCE_DATABASE_ALIAS)
        now = datetime.now(timezone.utc)

        # 1. Daily UUIDv7 partitions (Events + SpanStaging)
        daily_v7_models = [
            ("issue_events_issueevent", None),  # Use settings
            ("uptime_monitorcheck", None),
            ("logs_logevent", None),
            ("performance_spanstaging", None),
        ]
        start_date_daily = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date_daily = start_date_daily + timedelta(days=7)

        for table, buckets in daily_v7_models:
            if not manager.is_table_partitioned(table):
                self.stdout.write(f"Skipping {table} (not partitioned yet)...")
                continue
            self.stdout.write(f"Maintaining daily UUIDv7 partitions for {table}...")
            manager.create_partitions_for_date_range(
                parent_table=table,
                start_date=start_date_daily,
                end_date=end_date_daily,
                partition_interval="DAY",
                hash_buckets=buckets,
                hash_column="organization_id",
                key_type="uuid7",
            )

            # Cleanup old partitions
            # Skip logs and issue_events when cold storage handles archival-then-drop
            if "logs_logevent" in table:
                continue
            if "issue_events_issueevent" in table:
                from glitchtip.cold_storage import is_duckdb_available

                if is_duckdb_available():
                    continue

            max_days = settings.GLITCHTIP_EVENT_RETENTION_DAYS
            if "uptime" in table:
                max_days = settings.GLITCHTIP_UPTIME_RETENTION_DAYS
            elif "spanstaging" in table:
                max_days = 3  # Short retention: promotion drains rows quickly

            self.stdout.write(
                f"Cleaning up old partitions for {table} (retention: {max_days} days)..."
            )
            dropped = manager.drop_old_partitions(table, max_days)
            if dropped > 0:
                self.stdout.write(self.style.SUCCESS(f"Dropped {dropped} partitions."))

        # 2. Weekly DateTime partitions (Aggregates)
        weekly_models = [
            ("issue_events_issueaggregate", settings.GLITCHTIP_EVENT_RETENTION_DAYS),
            ("issue_events_issuetag", settings.GLITCHTIP_EVENT_RETENTION_DAYS),
            (
                "projects_issueeventprojecthourlystatistic",
                settings.GLITCHTIP_EVENT_RETENTION_DAYS,
            ),
            (
                "projects_transactioneventprojecthourlystatistic",
                settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS,
            ),
            (
                "projects_logprojecthourlystatistic",
                settings.GLITCHTIP_LOG_RETENTION_DAYS,
            ),
        ]
        start_of_week = start_date_daily - timedelta(days=start_date_daily.weekday())
        end_date_weekly = start_of_week + timedelta(weeks=4)

        for table, retention_days in weekly_models:
            if not manager.is_table_partitioned(table):
                self.stdout.write(f"Skipping {table} (not partitioned yet)...")
                continue
            self.stdout.write(f"Maintaining weekly partitions for {table}...")
            manager.create_partitions_for_date_range(
                parent_table=table,
                start_date=start_of_week,
                end_date=end_date_weekly,
                partition_interval="WEEK",
                hash_buckets=None,
                hash_column="organization_id",
                key_type="datetime",
            )

            # Cleanup old weekly partitions
            # partition_interval_days=7 ensures a partition isn't dropped until
            # its entire range is older than retention_days. With short retention
            # (e.g. 3 days), actual data lifetime is 7-10 days (best effort).
            self.stdout.write(f"Cleaning up old weekly partitions for {table}...")
            manager.drop_old_partitions(
                table, retention_days, partition_interval_days=7
            )

        self.stdout.write(self.style.SUCCESS("Partition maintenance complete."))

import logging
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand

from glitchtip.partition_manager import PartitionManager

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Create future partitions and cleanup old ones"

    def handle(self, *args, **options):
        manager = PartitionManager()
        now = datetime.now(timezone.utc)

        # 1. Daily UUIDv7 partitions (Events)
        daily_v7_models = [
            ("issue_events_issueevent", None),  # Use settings
            ("performance_transactionevent", None),  # Use settings
            ("uptime_monitorcheck", None),
            ("logs_logevent", None),
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

            max_days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS
            if "uptime" in table:
                max_days = settings.GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS
            elif "transaction" in table:
                max_days = settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS

            self.stdout.write(
                f"Cleaning up old partitions for {table} (retention: {max_days} days)..."
            )
            dropped = manager.drop_old_partitions(table, max_days)
            if dropped > 0:
                self.stdout.write(self.style.SUCCESS(f"Dropped {dropped} partitions."))

        # 2. Weekly DateTime partitions (Aggregates)
        weekly_models = [
            "issue_events_issueaggregate",
            "issue_events_issuetag",
            "performance_transactiongroupaggregate",
            "projects_issueeventprojecthourlystatistic",
            "projects_transactioneventprojecthourlystatistic",
            "projects_logprojecthourlystatistic",
        ]
        start_of_week = start_date_daily - timedelta(days=start_date_daily.weekday())
        end_date_weekly = start_of_week + timedelta(weeks=4)

        for table in weekly_models:
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

            # Cleanup old weekly partitions (using a default retention or specific one)
            # For aggregates, we can use GLITCHTIP_MAX_EVENT_LIFE_DAYS
            self.stdout.write(f"Cleaning up old weekly partitions for {table}...")
            manager.drop_old_partitions(table, settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)

        self.stdout.write(self.style.SUCCESS("Partition maintenance complete."))

"""
Management command to import legacy events from V1 to V2 (Storage Engine V2).

This command migrates events from the archived V1 table to the new V2 table with
dual-ID schema. It re-mints server IDs as UUIDv7 based on the `received` timestamp,
and preserves the original client-provided ID as `event_id`.

Usage:
    ./manage.py import_legacy_events [--batch-size=1000] [--dry-run]
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterator

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from glitchtip.partition_manager import PartitionManager, UUID7Helper

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Management command to import legacy events to partitioned tables (UUIDv7 partitioning)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Number of events to process per batch (default: 1000)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum number of events to import (default: all)",
        )
        parser.add_argument(
            "--start-date",
            type=str,
            default=None,
            help="Import events from this date onwards (ISO format: YYYY-MM-DD)",
        )
        parser.add_argument(
            "--end-date",
            type=str,
            default=None,
            help="Import events up to this date (ISO format: YYYY-MM-DD)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be imported without actually importing",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed progress information",
        )
        parser.add_argument(
            "--delete-source",
            action="store_true",
            help="Delete source table (issue_events_issueevent_archive) after successful import (DANGEROUS)",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        limit = options["limit"]
        dry_run = options["dry_run"]
        verbose = options["verbose"]
        start_date = options["start_date"]
        end_date = options["end_date"]
        delete_source = options["delete_source"]

        # Parse date filters
        start_dt = None
        end_dt = None
        if start_date:
            start_dt = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        if end_date:
            end_dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)

        self.stdout.write(self.style.WARNING("=" * 70))
        self.stdout.write(self.style.WARNING("Legacy Event Import"))
        self.stdout.write(self.style.WARNING("=" * 70))

        # Check if archive table exists
        if not self.table_exists("issue_events_issueevent_archive"):
            self.stdout.write(
                self.style.ERROR(
                    "Archive table 'issue_events_issueevent_archive' does not exist. "
                    "Have you run the migration?"
                )
            )
            return

        # Count total events to import
        total_count = self.count_events(start_dt, end_dt)
        if total_count == 0:
            self.stdout.write(
                self.style.WARNING("No events found in archive table to import.")
            )
            return

        events_to_import = min(total_count, limit) if limit else total_count

        self.stdout.write(f"\nFound {total_count} events in archive table")
        if limit:
            self.stdout.write(f"Will import: {events_to_import} events (limited)")
        else:
            self.stdout.write(f"Will import: {events_to_import} events")

        if start_dt:
            self.stdout.write(f"Start date: {start_dt.isoformat()}")
        if end_dt:
            self.stdout.write(f"End date: {end_dt.isoformat()}")

        self.stdout.write(f"Batch size: {batch_size}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\n*** DRY RUN MODE - No data will be imported ***\n"
                )
            )
            self.preview_migration(batch_size, start_dt, end_dt)
            return

        # Ensure partitions exist for the date range
        if not dry_run:
            self.stdout.write("\nChecking partitions...")
            import_start, import_end = self.get_date_range(start_dt, end_dt)
            if import_start and import_end:
                self.ensure_partitions(import_start, import_end)
            else:
                self.stdout.write(
                    "Could not determine date range. Skipping partition creation."
                )

        # Confirm before proceeding
        self.stdout.write(
            self.style.WARNING(
                "\nThis will import events into the V2 table. Continue? [y/N]: "
            ),
            ending="",
        )

        # Auto-confirm if non-interactive
        import sys

        if not sys.stdin.isatty():
            confirmation = "y"
            self.stdout.write("y (auto-confirmed in non-interactive mode)")
        else:
            confirmation = input().lower().strip()

        if confirmation != "y":
            self.stdout.write(self.style.ERROR("Import cancelled."))
            return

        # Perform the import
        self.stdout.write("\nStarting import...")
        imported_count = 0
        batch_num = 0

        try:
            for batch in self.fetch_events_batches(
                batch_size, events_to_import, start_dt, end_dt
            ):
                batch_num += 1
                with transaction.atomic():
                    count = self.import_batch(batch)
                    imported_count += count

                if verbose:
                    progress_pct = (imported_count / events_to_import) * 100
                    self.stdout.write(
                        f"Batch {batch_num}: Imported {count} events "
                        f"(Total: {imported_count}/{events_to_import}, {progress_pct:.1f}%)"
                    )
                elif batch_num % 10 == 0:
                    self.stdout.write(f"Progress: {imported_count}/{events_to_import}")

            self.stdout.write(
                self.style.SUCCESS(
                    f"\n✓ Successfully imported {imported_count} events to V2 table"
                )
            )

            if delete_source and not dry_run:
                self.stdout.write(self.style.WARNING("\nDeleting source table..."))
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DROP TABLE IF EXISTS issue_events_issueevent_archive CASCADE;"
                    )
                self.stdout.write(self.style.SUCCESS("✓ Source table deleted."))

        except Exception as e:
            logger.exception("Error during event import")
            self.stdout.write(self.style.ERROR(f"\n✗ Import failed: {e}"))
            raise

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database."""
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                );
                """,
                [table_name],
            )
            return cursor.fetchone()[0]

    def count_events(self, start_dt: datetime = None, end_dt: datetime = None) -> int:
        """Count events in archive table."""
        where_clauses = []
        params = []

        if start_dt:
            where_clauses.append("received >= %s")
            params.append(start_dt)
        if end_dt:
            where_clauses.append("received < %s")
            params.append(end_dt)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT COUNT(*) FROM issue_events_issueevent_archive {where_sql};",
                params,
            )
            return cursor.fetchone()[0]

    def preview_migration(
        self, batch_size: int, start_dt: datetime = None, end_dt: datetime = None
    ):
        """Show preview of what would be migrated."""
        self.stdout.write("\nPreview of first batch:")
        self.stdout.write("-" * 70)

        where_clauses = []
        params = []

        if start_dt:
            where_clauses.append("received >= %s")
            params.append(start_dt)
        if end_dt:
            where_clauses.append("received < %s")
            params.append(end_dt)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, received, issue_id, title
                FROM issue_events_issueevent_archive
                {where_sql}
                ORDER BY received
                LIMIT %s;
                """,
                params + [min(batch_size, 5)],
            )

            for row in cursor.fetchall():
                old_id, received, issue_id, title = row
                new_id = UUID7Helper.from_datetime(received)
                self.stdout.write(
                    f"  Old ID: {old_id} -> New ID: {new_id}\n"
                    f"  Received: {received}, Issue: {issue_id}\n"
                    f"  Title: {title[:50]}...\n"
                )

    def fetch_events_batches(
        self,
        batch_size: int,
        limit: int = None,
        start_dt: datetime = None,
        end_dt: datetime = None,
    ) -> Iterator[list]:
        """
        Fetch events from archive table in batches.

        Yields batches of event tuples ready for insertion.
        """
        offset = 0
        remaining = limit if limit else float("inf")

        where_clauses = []
        params = []

        if start_dt:
            where_clauses.append("received >= %s")
            params.append(start_dt)
        if end_dt:
            where_clauses.append("received < %s")
            params.append(end_dt)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        while remaining > 0:
            current_batch_size = min(batch_size, int(remaining))

            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        archive.id, archive.timestamp, archive.received,
                        archive.issue_id, archive.release_id,
                        archive.type, archive.level,
                        archive.title, archive.transaction, archive.data,
                        archive.tags, archive.hashes,
                        project.organization_id
                    FROM issue_events_issueevent_archive archive
                    JOIN issue_events_issue issue ON issue.id = archive.issue_id
                    JOIN projects_project project ON project.id = issue.project_id
                    {where_sql}
                    ORDER BY archive.received
                    LIMIT %s OFFSET %s;
                    """,
                    params + [current_batch_size, offset],
                )

                rows = cursor.fetchall()
                if not rows:
                    break

                yield rows

                offset += len(rows)
                remaining -= len(rows)

    def import_batch(self, batch: list) -> int:
        """
        Import a batch of events into the V2 table.

        For each event:
        - Generate new UUIDv7 ID based on `received` timestamp
        - Store original ID as `event_id`
        - Copy all other fields

        Returns:
            Number of events imported
        """
        if not batch:
            return 0

        # Prepare INSERT statement with column alignment
        insert_sql = """
        INSERT INTO issue_events_issueevent (
            id, event_id,
            timestamp, received,
            issue_id, release_id,
            type, level,
            title, transaction, data, tags, hashes,
            organization_id
        ) VALUES (
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s
        )
        ON CONFLICT DO NOTHING;
        """

        values = []
        for row in batch:
            (
                old_id,
                timestamp,
                received,
                issue_id,
                release_id,
                event_type,
                level,
                title,
                transaction,
                data,
                tags,
                hashes,
                organization_id,
            ) = row

            # Generate new UUIDv7 based on received timestamp
            new_id = UUID7Helper.from_datetime(received)

            # Prepare row for insertion
            values.append(
                (
                    new_id,  # id (server UUIDv7)
                    old_id,  # event_id (original client UUID)
                    timestamp,
                    received,
                    issue_id,
                    release_id,
                    event_type,
                    level,
                    title,
                    transaction,
                    data,
                    tags,
                    hashes,
                    organization_id,
                )
            )

        # Execute batch insert
        with connection.cursor() as cursor:
            cursor.executemany(insert_sql, values)
            return cursor.rowcount

    def get_date_range(
        self, start_dt: datetime = None, end_dt: datetime = None
    ) -> tuple[datetime, datetime]:
        """Get the min and max received dates for events to be imported."""
        if start_dt and end_dt:
            return start_dt, end_dt

        where_clauses = []
        params = []
        if start_dt:
            where_clauses.append("received >= %s")
            params.append(start_dt)
        if end_dt:
            where_clauses.append("received < %s")
            params.append(end_dt)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with connection.cursor() as cursor:
            # Get min date
            if not start_dt:
                cursor.execute(
                    f"SELECT MIN(received) FROM issue_events_issueevent_archive {where_sql}",
                    params,
                )
                start_dt = cursor.fetchone()[0]

            # Get max date
            if not end_dt:
                cursor.execute(
                    f"SELECT MAX(received) FROM issue_events_issueevent_archive {where_sql}",
                    params,
                )
                end_dt = cursor.fetchone()[0]

        return start_dt, end_dt

    def ensure_partitions(self, start_dt: datetime, end_dt: datetime):
        """Create partitions for the given date range."""
        if not start_dt or not end_dt:
            return

        # Normalize start_dt to midnight to align with daily partitions
        start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        # Add 1 day buffer to end date to cover the last day fully
        # PartitionManager treats end_date as exclusive, so if we have events up to
        # 2025-01-01 23:59:59, we need partition up to 2025-01-02
        adjusted_end_dt = end_dt.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)

        self.stdout.write(
            f"Ensuring partitions exist from {start_dt.date()} to {adjusted_end_dt.date()}..."
        )

        manager = PartitionManager()
        count = manager.create_partitions_for_date_range(
            parent_table="issue_events_issueevent",
            start_date=start_dt,
            end_date=adjusted_end_dt,
            partition_interval="DAY",
            hash_buckets=0,  # Simple partitioning (no hash)
            key_type="uuid7",
        )

        if count > 0:
            self.stdout.write(self.style.SUCCESS(f"Created {count} partitions."))
        else:
            self.stdout.write("Partitions already exist.")

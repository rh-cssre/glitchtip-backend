# Generated manually for Storage Engine V2
# Implements dual-ID schema (server UUIDv7 + client UUIDv4) with nested partitioning

import os

import django.contrib.postgres.fields
import apps.issue_events.models
from datetime import datetime, timedelta, timezone

from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState

from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions.
    Uses nested partitioning: RANGE (UUIDv7) -> HASH (organization_id).

    Determines start date by looking at legacy data to ensure we have partitions
    for the events we are about to migrate.
    """
    from glitchtip.partition_manager import PartitionManager

    event_limit = int(os.environ.get("GLITCHTIP_MIGRATION_EVENT_LIMIT", "1000"))
    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    now = datetime.now(timezone.utc)

    # Default start date is today
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Try to find older data in the archive to ensure we create partitions for it
    with schema_editor.connection.cursor() as cursor:
        try:
            # Check if archive table exists and has data
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT FROM pg_tables
                    WHERE schemaname = 'public'
                    AND tablename = 'issue_events_issueevent_archive'
                );
                """
            )
            if cursor.fetchone()[0]:
                # Find the oldest date among the recent events (matching migration logic)
                cursor.execute(
                    """
                    SELECT min(received)
                    FROM (
                        SELECT received
                        FROM issue_events_issueevent_archive
                        ORDER BY received DESC, id DESC
                        LIMIT %s
                    ) as sub;
                    """,
                    [event_limit],
                )
                min_received = cursor.fetchone()[0]
                if min_received:
                    # If we found data, ensure start_date covers it
                    if min_received.tzinfo is None:
                        min_received = min_received.replace(tzinfo=timezone.utc)
                    # Subtract 1 day as a safety buffer to ensure all rows are covered
                    min_date = (min_received - timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    if min_date < start_date:
                        start_date = min_date
                        print(
                            f"Adjusted partition start date to {start_date} to cover legacy events."
                        )
        except Exception as e:
            print(f"Warning: Could not determine legacy data range: {e}")

    # Ensure we cover at least 7 days from now
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=7
    )

    # If start_date is significantly in the past, end_date calculation should still ensure we cover up to now+7d
    # But manager.create_partitions_for_date_range iterates from start to end.
    # So valid range is [start_date, max(end_date, start_date + 7d? No, end_date is absolute)]

    # We want [start_date, now + 7 days]
    target_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=7
    )
    if end_date < target_end:
        end_date = target_end

    manager.create_partitions_for_date_range(
        parent_table="issue_events_issueevent",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Default safe value
        hash_column="organization_id",
        key_type="uuid7",
    )

    print(
        f"Created partitions for issue_events_issueevent from {start_date} to {end_date}"
    )


def migrate_legacy_data(apps, schema_editor):
    """
    Migrate recent events from the archive table to the new V2 table.
    Configurable via GLITCHTIP_MIGRATION_EVENT_LIMIT (default 1000).
    - Re-mints ID as UUIDv7 (preserving timestamp)
    - Sets event_id = old.id
    - Populates organization_id via join
    """
    from glitchtip.partition_manager import UUID7Helper

    event_limit = int(os.environ.get("GLITCHTIP_MIGRATION_EVENT_LIMIT", "1000"))
    insert_batch_size = 500

    with schema_editor.connection.cursor() as cursor:
        # Check if archive exists
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT FROM pg_tables
                WHERE schemaname = 'public'
                AND tablename = 'issue_events_issueevent_archive'
            );
            """
        )
        if not cursor.fetchone()[0]:
            return

        print(f"Migrating up to {event_limit} recent legacy events...")

        # Determine valid date range for partitions (matches create_initial_partitions)
        now = datetime.now(timezone.utc)
        start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

        cursor.execute(
            """
            SELECT min(received)
            FROM (
                SELECT received
                FROM issue_events_issueevent_archive
                ORDER BY received DESC, id DESC
                LIMIT %s
            ) as sub;
            """,
            [event_limit],
        )
        min_received = cursor.fetchone()[0]
        if min_received:
            if min_received.tzinfo is None:
                min_received = min_received.replace(tzinfo=timezone.utc)
            min_date = (min_received - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if min_date < start_date:
                start_date = min_date

        end_date = now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=7)

        print(
            f"Valid partition range: {start_date} to {end_date}"
        )

        # Fetch events
        cursor.execute(
            """
            SELECT
                archive.id, archive.timestamp, archive.received,
                archive.issue_id, archive.release_id,
                archive.type, archive.level,
                archive.title, archive.transaction, archive.data, archive.tags, archive.hashes,
                (SELECT project.organization_id FROM projects_project project JOIN issue_events_issue issue ON issue.project_id = project.id WHERE issue.id = archive.issue_id) as organization_id
            FROM issue_events_issueevent_archive archive
            ORDER BY archive.received DESC, archive.id DESC
            LIMIT %s
            """,
            [event_limit],
        )

        rows = cursor.fetchall()

        if not rows:
            print("No legacy events found.")
        else:
            insert_sql = """
                INSERT INTO issue_events_issueevent (
                    id, event_id, timestamp, issue_id, organization_id, release_id,
                    type, level, title, transaction, data, tags, hashes, created
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
            """

            total_migrated = 0
            skipped_count = 0
            batch = []

            for row in rows:
                (
                    old_id,
                    timestamp,
                    received,
                    issue_id,
                    release_id,
                    type_val,
                    level,
                    title,
                    transaction,
                    data,
                    tags,
                    hashes,
                    organization_id,
                ) = row

                if organization_id is None:
                    continue

                if received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)

                if received < start_date or received >= end_date:
                    skipped_count += 1
                    continue

                new_id = UUID7Helper.from_datetime(received)

                batch.append(
                    (
                        str(new_id),
                        str(old_id),
                        timestamp,
                        issue_id,
                        organization_id,
                        release_id,
                        type_val,
                        level,
                        title,
                        transaction,
                        data,
                        tags,
                        hashes,
                        received,
                    )
                )

                if len(batch) >= insert_batch_size:
                    cursor.executemany(insert_sql, batch)
                    total_migrated += len(batch)
                    batch = []

            if batch:
                cursor.executemany(insert_sql, batch)
                total_migrated += len(batch)

            del rows  # Free fetched data

            print(
                f"Migrated {total_migrated} events (skipped {skipped_count} out of range)."
            )

        # Cleanup
        retain_data = (
            os.environ.get("GLITCHTIP_RETAIN_LEGACY_DATA", "False").lower() == "true"
        )
        if not retain_data:
            print("Dropping legacy archive table...")
            cursor.execute(
                "DROP TABLE IF EXISTS issue_events_issueevent_archive CASCADE;"
            )
        else:
            print(
                "Skipping drop of issue_events_issueevent_archive (GLITCHTIP_RETAIN_LEGACY_DATA=True)"
            )


def drop_initial_partitions(apps, schema_editor):
    """
    Reverse migration: drop the partitions we created.
    """
    start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    with schema_editor.connection.cursor() as cursor:
        for day in range(7):
            partition_date = start_date + timedelta(days=day)
            partition_name = (
                f"issue_events_issueevent_{partition_date.strftime('%Y%m%d')}"
            )

            # Cascade drops all sub-partitions (hashes)
            sql = f"DROP TABLE IF EXISTS {partition_name} CASCADE;"
            cursor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0006_replace_tsvector_function"),
    ]

    operations = [
        # Phase 0: Create uuid_generate_v7() function
        RunSQL(
            sql=get_sql_content(__file__, "uuid_generate_v7.sql"),
            reverse_sql="DROP FUNCTION IF EXISTS uuid_generate_v7();",
        ),
        # Phase 1: Rename old table to archive (preserves data)
        SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                RunSQL(
                    sql="""
                    -- Rename existing events table to archive
                    ALTER TABLE IF EXISTS issue_events_issueevent
                    RENAME TO issue_events_issueevent_archive;

                    -- Rename indexes to avoid conflicts
                    ALTER INDEX IF EXISTS issue_events_issueevent_pkey
                    RENAME TO issue_events_issueevent_archive_pkey;

                    ALTER INDEX IF EXISTS issue_events_issueevent_issue_id_received_idx
                    RENAME TO issue_events_issueevent_archive_issue_id_received_idx;

                    ALTER INDEX IF EXISTS issue_events_issueevent_hashes_idx
                    RENAME TO issue_events_issueevent_archive_hashes_idx;

                    ALTER INDEX IF EXISTS issue_events_issueevent_release_id_idx
                    RENAME TO issue_events_issueevent_archive_release_id_idx;
                    """,
                    reverse_sql="""
                    -- Restore original table name
                    ALTER TABLE IF EXISTS issue_events_issueevent_archive
                    RENAME TO issue_events_issueevent;

                    -- Restore index names
                    ALTER INDEX IF EXISTS issue_events_issueevent_archive_pkey
                    RENAME TO issue_events_issueevent_pkey;

                    ALTER INDEX IF EXISTS issue_events_issueevent_archive_issue_id_received_idx
                    RENAME TO issue_events_issueevent_issue_id_received_idx;

                    ALTER INDEX IF EXISTS issue_events_issueevent_archive_hashes_idx
                    RENAME TO issue_events_issueevent_hashes_idx;

                    ALTER INDEX IF EXISTS issue_events_issueevent_archive_release_id_idx
                    RENAME TO issue_events_issueevent_release_id_idx;
                    """,
                ),
                # Drop foreign keys on archive table to prevent "cannot truncate" errors during tests
                RunSQL(
                    sql="""
                    DO $$
                    DECLARE
                        r record;
                    BEGIN
                        -- Drop all foreign key constraints on the archive table
                        FOR r IN
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = 'issue_events_issueevent_archive'::regclass
                            AND contype = 'f'
                        LOOP
                            EXECUTE 'ALTER TABLE issue_events_issueevent_archive DROP CONSTRAINT ' || quote_ident(r.conname);
                        END LOOP;
                    END
                    $$;
                    """,
                    reverse_sql="",  # No need to restore constraints on reverse, they were on the original table
                ),
            ],
        ),
        # Phase 2: Create V2 table with dual-ID schema
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="issueevent",
                    managers=[],
                ),
                migrations.AlterField(
                    model_name="issueevent",
                    name="hashes",
                    field=django.contrib.postgres.fields.ArrayField(
                        base_field=models.TextField(), db_default=[]
                    ),
                ),
                migrations.AddField(
                    model_name="issueevent",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.Organization",
                    ),
                ),
                migrations.AddField(
                    model_name="issueevent",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "id",
                        "organization",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                migrations.AlterField(
                    model_name="issueevent",
                    name="id",
                    field=models.UUIDField(
                        default=apps.issue_events.models._generate_uuid7,
                        editable=False,
                        help_text="Server-generated UUIDv7 (partition key, contains timestamp)",
                    ),
                ),
                migrations.AddField(
                    model_name="issueevent",
                    name="event_id",
                    field=models.UUIDField(
                        null=True,
                        blank=True,
                        db_index=True,
                        help_text="Client-provided event ID from Sentry SDK (UUIDv4)",
                    ),
                ),
            ],
            database_operations=[
                RunSQL(
                    sql=get_sql_content(__file__, "create_events_v2.sql"),
                    reverse_sql="""
                    DROP TABLE IF EXISTS issue_events_issueevent CASCADE;
                    """,
                ),
            ],
        ),
        # Phase 3: Create initial partitions
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_initial_partitions,
        ),
        # Phase 4: Migrate Data & Cleanup
        migrations.RunPython(
            code=migrate_legacy_data,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

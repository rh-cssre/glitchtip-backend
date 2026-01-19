# Generated manually for Storage Engine V2
# Replaces Uptime tables with V2 schema and migrates recent data

import apps.uptime.models
from datetime import datetime, timedelta, timezone
from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for MonitorCheck.
    Structure: Range (Day) -> Hash (organization_id)
    """
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    now = datetime.now(timezone.utc)

    # Default start date is today
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Try to find older data in the archive to ensure we create partitions for it
    with schema_editor.connection.cursor() as cursor:
        try:
            # Check if archive table exists
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT FROM pg_tables
                    WHERE schemaname = 'public'
                    AND tablename = 'uptime_monitorcheck_archive'
                );
                """
            )
            if cursor.fetchone()[0]:
                # Find the oldest date among the recent 10,000 checks
                cursor.execute(
                    """
                    SELECT min(start_check) 
                    FROM (
                        SELECT start_check 
                        FROM uptime_monitorcheck_archive 
                        ORDER BY start_check DESC, id DESC
                        LIMIT 10000
                    ) as sub;
                    """
                )
                min_start = cursor.fetchone()[0]
                if min_start:
                    if min_start.tzinfo is None:
                        min_start = min_start.replace(tzinfo=timezone.utc)
                    # Subtract 1 day as a safety buffer to ensure all 10k rows are covered
                    min_date = (min_start - timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    if min_date < start_date:
                        start_date = min_date
                        print(
                            f"Adjusted partition start date to {start_date} to cover legacy uptime checks."
                        )
        except Exception as e:
            print(f"Warning: Could not determine legacy uptime data range: {e}")

    # Ensure we cover at least 7 days from now
    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=7
    )

    manager.create_partitions_for_date_range(
        parent_table="uptime_monitorcheck",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=16,  # Default safe value
        hash_column="organization_id",
        key_type="uuid7",
    )

    print(f"Created partitions for uptime_monitorcheck from {start_date} to {end_date}")


def migrate_legacy_data(apps, schema_editor):
    """
    Migrate the most recent 10,000 uptime checks from the archive table.
    """
    from glitchtip.partition_manager import UUID7Helper
    import os

    with schema_editor.connection.cursor() as cursor:
        # Check if archive exists
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT FROM pg_tables
                WHERE schemaname = 'public'
                AND tablename = 'uptime_monitorcheck_archive'
            );
            """
        )
        if not cursor.fetchone()[0]:
            return

        print("Migrating recent legacy uptime checks...")

        # Fetch recent checks and join with Monitor to get organization_id
        cursor.execute(
            """
            SELECT
                archive.monitor_id, archive.start_check, archive.response_time,
                archive.reason, archive.is_up, archive.is_change, archive.data,
                monitor.organization_id
            FROM uptime_monitorcheck_archive archive
            JOIN uptime_monitor monitor ON monitor.id = archive.monitor_id
            ORDER BY archive.start_check DESC, archive.id DESC
            LIMIT 10000
            """
        )
        rows = cursor.fetchall()

        if not rows:
            print("No legacy uptime checks found.")
        else:
            values = []
            for row in rows:
                (
                    monitor_id,
                    start_check,
                    response_time,
                    reason,
                    is_up,
                    is_change,
                    data,
                    organization_id,
                ) = row

                # Re-mint ID as UUIDv7 using start_check time
                new_id = UUID7Helper.from_datetime(start_check)

                values.append(
                    (
                        str(new_id),
                        organization_id,
                        monitor_id,
                        start_check,
                        response_time,
                        reason,
                        is_up,
                        is_change,
                        data,
                    )
                )

            if values:
                insert_sql = """
                INSERT INTO uptime_monitorcheck (
                    id, organization_id, monitor_id, start_check,
                    response_time, reason, is_up, is_change, data
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
                """
                cursor.executemany(insert_sql, values)
                print(f"Migrated {len(values)} uptime checks.")

        # Cleanup
        retain_data = (
            os.environ.get("GLITCHTIP_RETAIN_LEGACY_DATA", "False").lower() == "true"
        )
        if not retain_data:
            print("Dropping legacy uptime archive table...")
            cursor.execute("DROP TABLE IF EXISTS uptime_monitorcheck_archive CASCADE;")
        else:
            print(
                "Skipping drop of uptime_monitorcheck_archive (GLITCHTIP_RETAIN_LEGACY_DATA=True)"
            )


def drop_partitions(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("uptime", "0001_squashed_0010_auto_20240712_1900"),
        ("issue_events", "0007_storage_v2_events"),  # For uuid_generate_v7 function
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="monitorcheck",
                    managers=[],
                ),
                migrations.AddField(
                    model_name="monitorcheck",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                migrations.AddField(
                    model_name="monitorcheck",
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
                    model_name="monitorcheck",
                    name="id",
                    field=models.UUIDField(
                        default=apps.uptime.models._generate_uuid7, editable=False
                    ),
                ),
            ],
            database_operations=[
                RunSQL(
                    sql="""
                    -- Rename existing table to archive
                    ALTER TABLE IF EXISTS uptime_monitorcheck
                    RENAME TO uptime_monitorcheck_archive;

                    -- Rename indexes
                    ALTER INDEX IF EXISTS uptime_monitorcheck_pkey
                    RENAME TO uptime_monitorcheck_archive_pkey;

                    ALTER INDEX IF EXISTS uptime_moni_monitor_a89b32_idx
                    RENAME TO uptime_moni_monitor_archive_a89b32_idx;

                    ALTER INDEX IF EXISTS uptime_moni_monitor_b6d442_idx
                    RENAME TO uptime_moni_monitor_archive_b6d442_idx;
                    """,
                    reverse_sql="""
                    ALTER TABLE IF EXISTS uptime_monitorcheck_archive
                    RENAME TO uptime_monitorcheck;
                    """,
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_uptime_v2.sql"),
                    reverse_sql="DROP TABLE IF EXISTS uptime_monitorcheck CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_partitions,
        ),
        migrations.RunPython(
            code=migrate_legacy_data,
            reverse_code=migrations.RunPython.noop,
        ),
    ]

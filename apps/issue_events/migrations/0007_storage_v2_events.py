# Generated manually for Storage Engine V2
# Implements dual-ID schema (server UUIDv7 + client UUIDv4) with nested partitioning

import django.contrib.postgres.fields
import apps.issue_events.models
from datetime import datetime, timedelta, UTC

from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState

from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions.
    Uses nested partitioning: RANGE (UUIDv7) -> HASH (organization_id).
    """
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    now = datetime.now(UTC)
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = start_date + timedelta(days=7)

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


def drop_initial_partitions(apps, schema_editor):
    """
    Reverse migration: drop the partitions we created.
    """
    start_date = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

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
        ("issue_events", "0001_squashed_0006_replace_tsvector_function"),
    ]

    operations = [
        # Phase 0: Create uuid_generate_v7() function
        RunSQL(
            sql=get_sql_content(__file__, "uuid_generate_v7.sql"),
            reverse_sql="DROP FUNCTION IF EXISTS uuid_generate_v7();",
        ),
        # Phase 1: Drop the pre-V2 table. It used to be renamed aside so the
        # data migration could drain the most recent events out of it; that
        # migration is gone (5.x cannot upgrade to 7.0 directly), so the table
        # and its now-pointless archive rename go with it.
        SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                RunSQL(
                    sql="DROP TABLE IF EXISTS issue_events_issueevent CASCADE;",
                    reverse_sql=RunSQL.noop,
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
    ]

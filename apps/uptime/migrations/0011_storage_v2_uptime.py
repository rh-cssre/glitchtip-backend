# Generated manually for Storage Engine V2
# Replaces Uptime tables with V2 schema and migrates recent data

import apps.uptime.models
from datetime import datetime, timedelta, UTC
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
    now = datetime.now(UTC)
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = start_date + timedelta(days=7)

    manager.create_partitions_for_date_range(
        parent_table="uptime_monitorcheck",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Default safe value
        hash_column="organization_id",
        key_type="uuid7",
    )

    print(f"Created partitions for uptime_monitorcheck from {start_date} to {end_date}")


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
                # The pre-V2 table used to be renamed aside so the data
                # migration could drain it; that migration is gone (5.x cannot
                # upgrade to 7.0 directly), so drop it instead of keeping an
                # empty archive around forever.
                RunSQL(
                    sql="DROP TABLE IF EXISTS uptime_monitorcheck CASCADE;",
                    reverse_sql=RunSQL.noop,
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
    ]

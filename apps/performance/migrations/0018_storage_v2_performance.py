# Generated manually for Storage Engine V2
# Replaces Performance tables with V2 schema (PartitionManager compliant)

import uuid
import apps.performance.models
from datetime import datetime, timedelta, timezone
from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for Performance tables.
    """
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)

    # 1. TransactionEvent (UUIDv7 Range -> Hash)
    # Create daily partitions for next 7 days
    start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_date = start_date + timedelta(days=7)

    manager.create_partitions_for_date_range(
        parent_table="performance_transactionevent",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Default from settings (16)
        hash_column="organization_id",
        key_type="uuid7",
    )

    # 2. TransactionGroupAggregate (Date Range -> Hash)
    # Create weekly partitions for next 4 weeks
    start_of_week = start_date - timedelta(days=start_date.weekday())
    end_date_agg = start_of_week + timedelta(weeks=4)

    manager.create_partitions_for_date_range(
        parent_table="performance_transactiongroupaggregate",
        start_date=start_of_week,
        end_date=end_date_agg,
        partition_interval="WEEK",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="datetime",
    )


def drop_partitions(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("performance", "0017_remove_transactionevent_duration_and_more"),
        ("issue_events", "0007_storage_v2_events"),  # For uuid_generate_v7 function
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="transactionevent",
                    managers=[],
                ),
                migrations.AlterModelManagers(
                    name="transactiongroupaggregate",
                    managers=[],
                ),
                migrations.AddField(
                    model_name="transactionevent",
                    name="duration",
                    field=models.PositiveIntegerField(
                        db_index=True, help_text="Milliseconds"
                    ),
                ),
                migrations.AddField(
                    model_name="transactionevent",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                migrations.AddField(
                    model_name="transactionevent",
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
                    model_name="transactionevent",
                    name="event_id",
                    field=models.UUIDField(
                        default=uuid.uuid4, editable=False, null=True
                    ),
                ),
                migrations.AlterField(
                    model_name="transactionevent",
                    name="id",
                    field=models.UUIDField(
                        default=apps.performance.models._generate_uuid7, editable=False
                    ),
                ),
                migrations.AddField(
                    model_name="transactiongroupaggregate",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                migrations.AddField(
                    model_name="transactiongroupaggregate",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "group",
                        "organization",
                        "date",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
            ],
            database_operations=[
                RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS performance_transactionevent CASCADE;
                    DROP TABLE IF EXISTS performance_transactiongroupaggregate CASCADE;
                    """,
                    reverse_sql="",
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_performance_v2.sql"),
                    reverse_sql="""
                    DROP TABLE IF EXISTS performance_transactionevent CASCADE;
                    DROP TABLE IF EXISTS performance_transactiongroupaggregate CASCADE;
                    """,
                ),
                # Ensure default partitions are attached properly (fixes check violation issues)
                RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS performance_transactionevent_default;
                    CREATE TABLE performance_transactionevent_default
                    PARTITION OF performance_transactionevent DEFAULT;

                    DROP TABLE IF EXISTS performance_transactiongroupaggregate_default;
                    CREATE TABLE performance_transactiongroupaggregate_default
                    PARTITION OF performance_transactiongroupaggregate DEFAULT;
                    """,
                    reverse_sql="",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_partitions,
        ),
    ]

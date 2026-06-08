# Generated manually for Storage Engine V2
# Replaces IssueAggregate with V2 schema (Range -> Hash partitioning)

from datetime import datetime, timedelta, timezone
from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for IssueAggregate.
    Structure: Range (Week) -> Hash (organization_id)
    """
    from glitchtip.partition_manager import PartitionManager

    # Start from current week start (Monday)
    now = datetime.now(timezone.utc)
    start_of_week = now - timedelta(days=now.weekday())
    start_date = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)

    # Create partitions for next 4 weeks (1 month coverage)
    end_date = start_date + timedelta(weeks=4)

    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    manager.create_partitions_for_date_range(
        parent_table="issue_events_issueaggregate",
        start_date=start_date,
        end_date=end_date,
        partition_interval="WEEK",
        hash_buckets=None,  # Use default from settings (16)
        hash_column="organization_id",
        key_type="datetime",
    )


def drop_partitions(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0007_storage_v2_events"),
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="issueaggregate",
                    managers=[],
                ),
                migrations.RemoveField(
                    model_name="issueaggregate",
                    name="pk",
                ),
                migrations.AddField(
                    model_name="issueaggregate",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "issue",
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
                    sql="DROP TABLE IF EXISTS issue_events_issueaggregate CASCADE;",
                    reverse_sql="",  # Irreversible data loss (Fresh Start)
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_issue_aggregate_v2.sql"),
                    reverse_sql="DROP TABLE IF EXISTS issue_events_issueaggregate CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_partitions,
        ),
    ]

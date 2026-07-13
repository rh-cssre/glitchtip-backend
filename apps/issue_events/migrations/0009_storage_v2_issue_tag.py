# Generated manually for Storage Engine V2
# Replaces IssueTag with V2 schema

from datetime import datetime, timedelta, timezone
from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for IssueTag.
    Structure: Range (Week) -> Hash (organization_id)
    """
    from glitchtip.partition_manager import PartitionManager

    now = datetime.now(timezone.utc)
    start_of_week = now - timedelta(days=now.weekday())
    start_date = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)

    # Create partitions for next 4 weeks
    end_date = start_date + timedelta(weeks=4)

    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    manager.create_partitions_for_date_range(
        parent_table="issue_events_issuetag",
        start_date=start_date,
        end_date=end_date,
        partition_interval="WEEK",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="datetime",
    )


def drop_partitions(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0008_storage_v2_aggregates"),
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="issuetag",
                    managers=[],
                ),
                migrations.RemoveConstraint(
                    model_name="issuetag",
                    name="issue_tag_key_value_unique",
                ),
                migrations.AddField(
                    model_name="issuetag",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE, to="organizations_ext.organization"
                    ),
                ),
                migrations.AddField(
                    model_name="issuetag",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "issue",
                        "organization",
                        "date",
                        "tag_key",
                        "tag_value",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                migrations.RemoveField(
                    model_name="issuetag",
                    name="id",
                ),
            ],
            database_operations=[
                RunSQL(
                    sql="DROP TABLE IF EXISTS issue_events_issuetag CASCADE;",
                    reverse_sql="",
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_issue_tag_v2.sql"),
                    reverse_sql="DROP TABLE IF EXISTS issue_events_issuetag CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_partitions,
        ),
    ]

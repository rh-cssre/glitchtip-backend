# Generated manually for Storage Engine V2
# Replaces Project Statistics with V2 schema

from datetime import datetime, timedelta, timezone
from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState
from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """
    Create initial partitions for Project Stats.
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
        parent_table="projects_issueeventprojecthourlystatistic",
        start_date=start_date,
        end_date=end_date,
        partition_interval="WEEK",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="datetime",
    )

    manager.create_partitions_for_date_range(
        parent_table="projects_transactioneventprojecthourlystatistic",
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
        ("projects", "0018_auto_20251117_2129"),
        ("issue_events", "0007_storage_v2_events"),
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelManagers(
                    name="issueeventprojecthourlystatistic",
                    managers=[],
                ),
                migrations.AlterModelManagers(
                    name="transactioneventprojecthourlystatistic",
                    managers=[],
                ),
                migrations.AlterUniqueTogether(
                    name="issueeventprojecthourlystatistic",
                    unique_together=set(),
                ),
                migrations.AlterUniqueTogether(
                    name="transactioneventprojecthourlystatistic",
                    unique_together=set(),
                ),
                migrations.AddField(
                    model_name="issueeventprojecthourlystatistic",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                migrations.AddField(
                    model_name="issueeventprojecthourlystatistic",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "project",
                        "organization",
                        "date",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                migrations.AddField(
                    model_name="transactioneventprojecthourlystatistic",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
                migrations.AddField(
                    model_name="transactioneventprojecthourlystatistic",
                    name="pk",
                    field=models.CompositePrimaryKey(
                        "project",
                        "organization",
                        "date",
                        blank=True,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                migrations.RemoveField(
                    model_name="issueeventprojecthourlystatistic",
                    name="id",
                ),
                migrations.RemoveField(
                    model_name="transactioneventprojecthourlystatistic",
                    name="id",
                ),
            ],
            database_operations=[
                RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS projects_issueeventprojecthourlystatistic CASCADE;
                    DROP TABLE IF EXISTS projects_transactioneventprojecthourlystatistic CASCADE;
                    """,
                    reverse_sql="",
                ),
                RunSQL(
                    sql=get_sql_content(__file__, "create_project_stats_v2.sql"),
                    reverse_sql="""
                    DROP TABLE IF EXISTS projects_issueeventprojecthourlystatistic CASCADE;
                    DROP TABLE IF EXISTS projects_transactioneventprojecthourlystatistic CASCADE;
                    """,
                ),
                # Ensure default partitions attached
                RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS projects_issueeventprojecthourlystatistic_default;
                    CREATE TABLE projects_issueeventprojecthourlystatistic_default
                    PARTITION OF projects_issueeventprojecthourlystatistic DEFAULT;

                    DROP TABLE IF EXISTS projects_transactioneventprojecthourlystatistic_default;
                    CREATE TABLE projects_transactioneventprojecthourlystatistic_default
                    PARTITION OF projects_transactioneventprojecthourlystatistic DEFAULT;
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

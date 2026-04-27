# Generated manually for UptimeCheckHourlyStatistic

from datetime import datetime, timedelta, timezone

from django.db import migrations, models
from django.db.migrations import RunSQL, SeparateDatabaseAndState

from apps.shared.migration_utils import get_sql_content


def create_initial_partitions(apps, schema_editor):
    """Create initial weekly partitions for UptimeCheckHourlyStatistic."""
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)
    now = datetime.now(timezone.utc)

    start_of_week = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_week -= timedelta(days=start_of_week.weekday())
    end_date = start_of_week + timedelta(weeks=4)

    manager.create_partitions_for_date_range(
        parent_table="uptime_uptimecheckhourlystatistic",
        start_date=start_of_week,
        end_date=end_date,
        partition_interval="WEEK",
        hash_buckets=None,
        hash_column="organization_id",
        key_type="datetime",
    )
    print(
        f"Created weekly partitions for uptime_uptimecheckhourlystatistic "
        f"from {start_of_week} to {end_date}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("uptime", "0014_backfill_cached_fields"),
        ("organizations_ext", "0011_organization_is_deleted"),
    ]

    operations = [
        SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="UptimeCheckHourlyStatistic",
                    fields=[
                        (
                            "organization",
                            models.ForeignKey(
                                on_delete=models.CASCADE,
                                to="organizations_ext.organization",
                            ),
                        ),
                        ("date", models.DateTimeField()),
                        ("count", models.PositiveIntegerField()),
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "organization",
                                "date",
                                blank=True,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                    ],
                ),
            ],
            database_operations=[
                RunSQL(
                    sql=get_sql_content(__file__, "create_uptime_check_stats.sql"),
                    reverse_sql="DROP TABLE IF EXISTS uptime_uptimecheckhourlystatistic CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(create_initial_partitions, migrations.RunPython.noop),
    ]

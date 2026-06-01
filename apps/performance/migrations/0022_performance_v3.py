# Performance Monitoring V3: Span-level tracking
# - Drop TransactionEvent and TransactionGroupAggregate
# - Recreate TransactionGroup as hash-partitioned by organization_id
# - Create SpanStaging partitioned table (UUID7 + HASH by org)
#
# The previous migration (0021) already detached and dropped all child
# partitions, so the DROP TABLE CASCADE statements here only touch the
# empty parent tables — well within max_locks_per_transaction.

from datetime import datetime, timedelta, timezone

import apps.performance.models
from django.conf import settings
from django.db import migrations, models

from apps.shared.migration_utils import get_sql_content


def create_transaction_group_hash_partitions(apps, schema_editor):
    """Create hash partitions for TransactionGroup based on settings."""
    hash_buckets = settings.PARTITION_HASH_BUCKETS
    with schema_editor.connection.cursor() as cursor:
        for i in range(hash_buckets):
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS performance_transactiongroup_h{i} "
                f"PARTITION OF performance_transactiongroup "
                f"FOR VALUES WITH (MODULUS {hash_buckets}, REMAINDER {i});"
            )


def create_initial_span_partitions(apps, schema_editor):
    """Create initial daily UUID7 partitions with HASH sub-partitioning."""
    from glitchtip.partition_manager import PartitionManager

    manager = PartitionManager(db_connection=schema_editor.connection.alias)

    start_date = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_date = start_date + timedelta(days=7)

    manager.create_partitions_for_date_range(
        parent_table="performance_spanstaging",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Use settings.PARTITION_HASH_BUCKETS
        hash_column="organization_id",
        key_type="uuid7",
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("performance", "0021_detach_old_partitions"),
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
    ]

    operations = [
        # 1. Drop old (now-empty) parent tables.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="TransactionEvent"),
                migrations.DeleteModel(name="TransactionGroupAggregate"),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DROP TABLE IF EXISTS performance_transactionevent CASCADE;
                    DROP TABLE IF EXISTS performance_transactiongroupaggregate CASCADE;
                    """,
                    reverse_sql="",
                ),
            ],
        ),
        # 2. Recreate TransactionGroup as hash-partitioned by organization_id.
        #    Raw SQL drops the old table and creates the partitioned replacement.
        #    Hash child partitions are created by RunPython (step 3) so the
        #    bucket count follows settings.PARTITION_HASH_BUCKETS.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # Remove old fields
                migrations.RemoveField(
                    model_name="transactiongroup",
                    name="tags",
                ),
                migrations.RemoveField(
                    model_name="transactiongroup",
                    name="search_vector",
                ),
                migrations.RemoveField(
                    model_name="transactiongroup",
                    name="is_deleted",
                ),
                # Add composite PK and explicit id
                migrations.AddField(
                    model_name="transactiongroup",
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
                    model_name="transactiongroup",
                    name="id",
                    field=models.BigIntegerField(db_default=0, editable=False),
                ),
                # Add new fields
                migrations.AddField(
                    model_name="transactiongroup",
                    name="organization",
                    field=models.ForeignKey(
                        on_delete=models.deletion.DO_NOTHING,
                        to="organizations_ext.organization",
                    ),
                    preserve_default=False,
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="first_seen",
                    field=models.DateTimeField(),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="last_seen",
                    field=models.DateTimeField(),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="avg_duration",
                    field=models.FloatField(
                        default=0, help_text="Average duration in ms"
                    ),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="p50",
                    field=models.FloatField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="p95",
                    field=models.FloatField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="count",
                    field=models.PositiveBigIntegerField(default=0),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="error_count",
                    field=models.PositiveBigIntegerField(default=0),
                ),
                migrations.AddField(
                    model_name="transactiongroup",
                    name="duration_histogram",
                    field=models.JSONField(default=dict),
                ),
                # Change FKs to DO_NOTHING (no DB-level constraints for
                # partitioned tables)
                migrations.AlterField(
                    model_name="transactiongroup",
                    name="project",
                    field=models.ForeignKey(
                        on_delete=models.deletion.DO_NOTHING,
                        to="projects.project",
                    ),
                ),
                # Update unique constraint to include organization (required
                # by PG for hash-partitioned tables)
                migrations.RemoveConstraint(
                    model_name="transactiongroup",
                    name="unique_transaction_project_op_method",
                ),
                migrations.AddConstraint(
                    model_name="transactiongroup",
                    constraint=models.UniqueConstraint(
                        fields=[
                            "transaction",
                            "project",
                            "op",
                            "method",
                            "organization",
                        ],
                        name="unique_transaction_project_op_method",
                    ),
                ),
                # Align method default with SQL's DEFAULT ''
                migrations.AlterField(
                    model_name="transactiongroup",
                    name="method",
                    field=models.CharField(blank=True, default="", max_length=255),
                ),
                # Remove old managers (SoftDeleteModel)
                migrations.AlterModelManagers(
                    name="transactiongroup",
                    managers=[],
                ),
                # Add index
                migrations.AddIndex(
                    model_name="transactiongroup",
                    index=models.Index(
                        fields=["organization", "last_seen"],
                        name="perf_txgroup_org_lastseen",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=get_sql_content(__file__, "create_transaction_group_v3.sql"),
                    reverse_sql="DROP TABLE IF EXISTS performance_transactiongroup CASCADE;",
                ),
            ],
        ),
        # 3. Create hash child partitions (count from settings)
        migrations.RunPython(
            code=create_transaction_group_hash_partitions,
            reverse_code=noop,
        ),
        # 4. Create SpanStaging partitioned table (UUID7 range + HASH org)
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="SpanStaging",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=apps.performance.models._generate_uuid7,
                                editable=False,
                            ),
                        ),
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "id",
                                "organization",
                                blank=True,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        ("project_id", models.IntegerField()),
                        (
                            "transaction_name",
                            models.CharField(max_length=1024),
                        ),
                        ("span_id", models.CharField(max_length=32)),
                        ("transaction_id", models.CharField(max_length=32)),
                        ("op", models.CharField(max_length=255)),
                        (
                            "description",
                            models.CharField(blank=True, default="", max_length=500),
                        ),
                        (
                            "duration",
                            models.FloatField(help_text="Duration in milliseconds"),
                        ),
                        (
                            "timestamp",
                            models.DateTimeField(help_text="Span start time"),
                        ),
                        (
                            "organization",
                            models.ForeignKey(
                                on_delete=models.deletion.DO_NOTHING,
                                to="organizations_ext.organization",
                            ),
                        ),
                    ],
                    options={
                        "db_table": "performance_spanstaging",
                    },
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=get_sql_content(__file__, "create_span_staging.sql"),
                    reverse_sql="DROP TABLE IF EXISTS performance_spanstaging CASCADE;",
                ),
            ],
        ),
        # 5. Create initial daily partitions (UUID7 + HASH by org)
        migrations.RunPython(
            code=create_initial_span_partitions,
            reverse_code=noop,
        ),
    ]

# Generated manually for Logs feature
# Squashed from 0001_initial + 0002_add_search_indexes + 0003_add_log_service_lookup
#
# Implements UUIDv7 partitioning with nested HASH by organization_id,
# search indexes (trigram + service/environment/host), and LogResource lookup table.

from datetime import datetime, timedelta, timezone

import django.db.models.deletion
from django.contrib.postgres.operations import TrigramExtension
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
    now = datetime.now(timezone.utc)

    # Start date is today
    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Create partitions for 7 days ahead
    end_date = start_date + timedelta(days=7)

    manager.create_partitions_for_date_range(
        parent_table="logs_logevent",
        start_date=start_date,
        end_date=end_date,
        partition_interval="DAY",
        hash_buckets=None,  # Uses settings.PARTITION_HASH_BUCKETS
        hash_column="organization_id",
        key_type="uuid7",
    )

    print(f"Created partitions for logs_logevent from {start_date} to {end_date}")


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
            partition_name = f"logs_logevent_{partition_date.strftime('%Y%m%d')}"

            # Cascade drops all sub-partitions (hashes)
            sql = f"DROP TABLE IF EXISTS {partition_name} CASCADE;"
            cursor.execute(sql)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("organizations_ext", "0001_squashed_0008_merge_20250210_1625"),
        ("organizations_ext", "0010_alter_organization_id"),
        ("projects", "0001_squashed_0016_auto_20250125_1733"),
        # Ensure uuid_generate_v7() function exists
        ("issue_events", "0007_storage_v2_events"),
    ]

    operations = [
        # Ensure pg_trgm extension is available for GIN trigram indexes
        TrigramExtension(),
        # Phase 1: Create partitioned table (SQL includes all indexes)
        SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="LogEvent",
                    fields=[
                        # 16-byte alignment: UUIDs
                        (
                            "id",
                            models.UUIDField(
                                editable=False,
                                help_text="UUIDv7 generated from client timestamp (partition key)",
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
                        (
                            "trace_id",
                            models.UUIDField(
                                blank=True,
                                help_text="Trace ID for correlating logs with traces",
                                null=True,
                            ),
                        ),
                        # 8-byte alignment: ForeignKeys and BigInt
                        (
                            "organization",
                            models.ForeignKey(
                                on_delete=models.deletion.CASCADE,
                                to="organizations_ext.organization",
                            ),
                        ),
                        (
                            "project",
                            models.ForeignKey(
                                on_delete=models.deletion.CASCADE,
                                to="projects.project",
                            ),
                        ),
                        (
                            "span_id",
                            models.BigIntegerField(
                                blank=True,
                                help_text="Span ID for correlating logs with specific spans (8-byte)",
                                null=True,
                            ),
                        ),
                        # 2-byte alignment: SmallIntegers
                        (
                            "level",
                            models.PositiveSmallIntegerField(
                                choices=[
                                    (0, "trace"),
                                    (1, "debug"),
                                    (2, "info"),
                                    (3, "warn"),
                                    (4, "error"),
                                    (5, "fatal"),
                                ],
                                default=2,
                                help_text="Log level (trace/debug/info/warn/error/fatal)",
                            ),
                        ),
                        (
                            "severity_number",
                            models.PositiveSmallIntegerField(
                                blank=True,
                                help_text="OpenTelemetry severity number (1-24)",
                                null=True,
                            ),
                        ),
                        # Variable-width fields
                        (
                            "body",
                            models.TextField(help_text="The log message body"),
                        ),
                        (
                            "service",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Service name that emitted the log",
                                max_length=255,
                            ),
                        ),
                        (
                            "environment",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Deployment environment (e.g. production, staging)",
                                max_length=255,
                            ),
                        ),
                        (
                            "host",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Host name that emitted the log",
                                max_length=255,
                            ),
                        ),
                        (
                            "data",
                            models.JSONField(
                                blank=True,
                                default=dict,
                                help_text="Additional structured data/attributes",
                            ),
                        ),
                    ],
                    options={
                        "indexes": [
                            models.Index(
                                fields=["organization", "-id"],
                                name="logevent_org_id_idx",
                            ),
                            models.Index(
                                fields=["project", "-id"],
                                name="logevent_proj_id_idx",
                            ),
                            models.Index(
                                fields=["organization", "level", "-id"],
                                name="logevent_org_level_idx",
                            ),
                            models.Index(
                                fields=["organization", "service", "-id"],
                                name="logevent_org_svc_idx",
                            ),
                            models.Index(
                                fields=["organization", "environment", "-id"],
                                name="logevent_org_env_idx",
                            ),
                            models.Index(
                                fields=["organization", "host", "-id"],
                                name="logevent_org_host_idx",
                            ),
                            models.Index(
                                condition=models.Q(trace_id__isnull=False),
                                fields=["trace_id"],
                                name="logevent_trace_id_idx",
                            ),
                        ],
                    },
                ),
            ],
            database_operations=[
                RunSQL(
                    sql=get_sql_content(__file__, "create_logs_v1.sql"),
                    reverse_sql="""
                    DROP TABLE IF EXISTS logs_logevent CASCADE;
                    """,
                ),
            ],
        ),
        # Phase 2: Create initial partitions
        migrations.RunPython(
            code=create_initial_partitions,
            reverse_code=drop_initial_partitions,
        ),
        # Phase 3: LogResource lookup table
        migrations.CreateModel(
            name="LogResource",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(help_text="Resource name", max_length=255)),
                (
                    "type",
                    models.CharField(
                        choices=[
                            ("service", "service"),
                            ("environment", "environment"),
                            ("host", "host"),
                        ],
                        default="service",
                        help_text="Type of resource (service/environment/host)",
                        max_length=20,
                    ),
                ),
                ("first_seen", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="organizations_ext.organization",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["organization", "type"],
                        name="logresource_org_type_idx",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "name", "type"),
                        name="unique_org_resource",
                    )
                ],
            },
        ),
    ]

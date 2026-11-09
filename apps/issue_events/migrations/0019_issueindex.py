"""Create IssueIndex: hash-partitioned search table for issues.

Decouples full-text search from the Issue table so GIN index maintenance
doesn't impact the hot ingest write path.
"""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from apps.shared.migration_utils import get_sql_content


def create_index_hash_partitions(apps, schema_editor):
    """Create hash partitions for IssueIndex based on settings."""
    hash_buckets = settings.PARTITION_HASH_BUCKETS
    with schema_editor.connection.cursor() as cursor:
        for i in range(hash_buckets):
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS issue_events_issueindex_h{i} "
                f"PARTITION OF issue_events_issueindex "
                f"FOR VALUES WITH (MODULUS {hash_buckets}, REMAINDER {i});"
            )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0018_issue_assignment"),
        ("organizations_ext", "0011_organization_is_deleted"),
        ("releases", "0009_release_repository"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="IssueIndex",
                    fields=[
                        (
                            "issue",
                            models.OneToOneField(
                                db_constraint=False,
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="index",
                                to="issue_events.issue",
                            ),
                        ),
                        (
                            "last_release",
                            models.ForeignKey(
                                blank=True,
                                db_constraint=False,
                                db_index=False,
                                null=True,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                related_name="+",
                                to="releases.release",
                            ),
                        ),
                        (
                            "last_seen",
                            models.DateTimeField(
                                default=django.utils.timezone.now, editable=False
                            ),
                        ),
                        (
                            "organization",
                            models.ForeignKey(
                                db_constraint=False,
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                to="organizations_ext.organization",
                            ),
                        ),
                        (
                            "count",
                            models.PositiveIntegerField(default=1, editable=False),
                        ),
                        (
                            "status",
                            models.PositiveSmallIntegerField(
                                choices=[
                                    (0, "unresolved"),
                                    (1, "resolved"),
                                    (2, "ignored"),
                                ],
                                default=0,
                            ),
                        ),
                        (
                            "level",
                            models.PositiveSmallIntegerField(
                                choices=[
                                    (0, "sample"),
                                    (1, "debug"),
                                    (2, "info"),
                                    (3, "warning"),
                                    (4, "error"),
                                    (5, "fatal"),
                                ],
                                default=4,
                            ),
                        ),
                        (
                            "pk",
                            models.CompositePrimaryKey(
                                "issue",
                                "organization",
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "fts_document",
                            django.contrib.postgres.search.SearchVectorField(
                                default="", editable=False
                            ),
                        ),
                    ],
                    options={
                        "indexes": [
                            django.contrib.postgres.indexes.GinIndex(
                                fields=["fts_document"],
                                name="issueindex_fts_gin",
                            ),
                        ],
                    },
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=get_sql_content(__file__, "create_issue_index.sql"),
                    reverse_sql="DROP TABLE IF EXISTS issue_events_issueindex CASCADE;",
                ),
            ],
        ),
        migrations.RunPython(
            code=create_index_hash_partitions,
            reverse_code=noop,
        ),
    ]

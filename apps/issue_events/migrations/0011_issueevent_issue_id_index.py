# Optimize IssueEvent indexes for partition pruning and rename to shorter names.
#
# Changes:
# 1. Replace issue+received index with issue+id for partition pruning
#    (UUIDv7 ordering is equivalent to time ordering)
# 2. Rename all indexes to shorter names (under 30 chars for Django checks)
#
# For new installs: create_events_v2.sql already has the new indexes/names,
# so database operations use IF NOT EXISTS / IF EXISTS for idempotency.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0010_alter_issueevent_event_id_and_more"),
    ]

    operations = [
        # 1. Remove old issue+received index, add new issue+id index
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(
                    model_name="issueevent",
                    name="issue_event_issue_i_3b4e7f_idx",
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="DROP INDEX IF EXISTS issue_events_issueevent_issue_received_idx;",
                    reverse_sql="CREATE INDEX IF NOT EXISTS issue_events_issueevent_issue_received_idx ON issue_events_issueevent (issue_id, received DESC);",
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="issueevent",
                    index=models.Index(
                        fields=["issue", "-id"],
                        name="issueevent_issue_id_idx",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="CREATE INDEX IF NOT EXISTS issueevent_issue_id_idx ON issue_events_issueevent (issue_id, id DESC);",
                    reverse_sql="DROP INDEX IF EXISTS issueevent_issue_id_idx;",
                ),
            ],
        ),
        # 2. Rename release index to shorter name
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(
                    model_name="issueevent",
                    name="issue_events_issueevent_release_id_idx",
                ),
                migrations.AddIndex(
                    model_name="issueevent",
                    index=models.Index(
                        fields=["release"],
                        name="issueevent_release_idx",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DROP INDEX IF EXISTS issue_events_issueevent_release_id_idx;
                    CREATE INDEX IF NOT EXISTS issueevent_release_idx ON issue_events_issueevent (release_id);
                    """,
                    reverse_sql="""
                    DROP INDEX IF EXISTS issueevent_release_idx;
                    CREATE INDEX IF NOT EXISTS issue_events_issueevent_release_id_idx ON issue_events_issueevent (release_id);
                    """,
                ),
            ],
        ),
        # 3. Rename event_id index to shorter name
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveIndex(
                    model_name="issueevent",
                    name="issue_events_issueevent_event_id_idx",
                ),
                migrations.AddIndex(
                    model_name="issueevent",
                    index=models.Index(
                        condition=models.Q(event_id__isnull=False),
                        fields=["event_id"],
                        name="issueevent_event_id_idx",
                    ),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DROP INDEX IF EXISTS issue_events_issueevent_event_id_idx;
                    CREATE INDEX IF NOT EXISTS issueevent_event_id_idx ON issue_events_issueevent (event_id) WHERE event_id IS NOT NULL;
                    """,
                    reverse_sql="""
                    DROP INDEX IF EXISTS issueevent_event_id_idx;
                    CREATE INDEX IF NOT EXISTS issue_events_issueevent_event_id_idx ON issue_events_issueevent (event_id) WHERE event_id IS NOT NULL;
                    """,
                ),
            ],
        ),
        # 4. Rename hashes GIN index to shorter name (state-only, DB handled by SQL file)
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunSQL(
                    sql="""
                    DROP INDEX IF EXISTS issue_events_issueevent_hashes_idx;
                    CREATE INDEX IF NOT EXISTS issueevent_hashes_idx ON issue_events_issueevent USING GIN (hashes);
                    """,
                    reverse_sql="""
                    DROP INDEX IF EXISTS issueevent_hashes_idx;
                    CREATE INDEX IF NOT EXISTS issue_events_issueevent_hashes_idx ON issue_events_issueevent USING GIN (hashes);
                    """,
                ),
            ],
        ),
    ]

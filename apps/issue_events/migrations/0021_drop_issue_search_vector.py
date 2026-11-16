"""Drop the now-unused Issue.search_vector column.

Full-text search reads IssueIndex (populated on ingest + backfilled in
0020); nothing reads or writes Issue.search_vector anymore. Removing it
narrows the hot Issue row (better cache density for issue-list reads) and
reclaims the column from a table too large to hash-partition today -- and
smaller for it, should we ever want to.

DROP COLUMN in Postgres is metadata-only (no table rewrite, O(1) regardless
of row count); the existing column bytes are reclaimed lazily as rows are
next updated. The only cost is the brief ACCESS EXCLUSIVE lock. We bound the
wait with ``lock_timeout`` so a busy moment fails fast and is retried, rather
than queueing behind a long query and stalling ingest. The GIN was already
dropped in 0020, so this does not cascade to a large index.

DEPLOY ORDER: this column must be gone only AFTER all code that reads/writes
it has stopped running. Application code in this change no longer touches it,
so drain the old release before applying this migration.
"""

from django.db import migrations


class Migration(migrations.Migration):
    # DROP COLUMN is metadata-only and fast, so an atomic migration is fine.
    # SET LOCAL scopes the lock_timeout to this migration's transaction.
    dependencies = [
        ("issue_events", "0020_backfill_and_drop_search_gin"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="issue", name="search_vector"),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "SET LOCAL lock_timeout = '5s'; "
                        "ALTER TABLE issue_events_issue DROP COLUMN search_vector;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE issue_events_issue "
                        "ADD COLUMN search_vector tsvector NOT NULL DEFAULT '';"
                    ),
                ),
            ],
        ),
    ]

"""Drop the hot-split columns from Issue.

count / last_seen / status / level / last_release now live on IssueIndex
(created in 0019, backfilled in 0020). The application reads them through Issue's
proxy properties (``issue.index.*``) and writes them to the leaf directly,
so these columns on Issue are dead weight. Dropping them makes Issue a small,
near-static table that is no longer rewritten on every event.

DROP COLUMN in Postgres is metadata-only (O(1), no table rewrite); the column
bytes are reclaimed lazily as rows are next updated. The only cost is the brief
ACCESS EXCLUSIVE lock, bounded by ``lock_timeout`` so a busy moment fails fast
and is retried rather than queueing behind a long query and stalling ingest.

DEPLOY ORDER: the *previous* release writes these columns on every event
(count/last_seen) and on resolve (status). Drain the old release before applying
this migration, or hold it for a follow-up deploy once 0019/0020 have rolled out
and the new leaf write path is live.
"""

from django.db import migrations


class Migration(migrations.Migration):
    # DROP COLUMN is metadata-only and fast, so an atomic migration is fine;
    # SET LOCAL scopes lock_timeout to this migration's transaction.
    dependencies = [
        ("issue_events", "0021_drop_issue_search_vector"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="issue", name="count"),
                migrations.RemoveField(model_name="issue", name="last_release"),
                migrations.RemoveField(model_name="issue", name="last_seen"),
                migrations.RemoveField(model_name="issue", name="level"),
                migrations.RemoveField(model_name="issue", name="status"),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        "SET LOCAL lock_timeout = '5s'; "
                        "ALTER TABLE issue_events_issue "
                        "DROP COLUMN count, "
                        "DROP COLUMN last_release_id, "
                        "DROP COLUMN last_seen, "
                        "DROP COLUMN level, "
                        "DROP COLUMN status;"
                    ),
                    reverse_sql=(
                        "ALTER TABLE issue_events_issue "
                        "ADD COLUMN count integer NOT NULL DEFAULT 1, "
                        "ADD COLUMN last_release_id bigint, "
                        "ADD COLUMN last_seen timestamptz NOT NULL DEFAULT now(), "
                        "ADD COLUMN level smallint NOT NULL DEFAULT 4, "
                        "ADD COLUMN status smallint NOT NULL DEFAULT 0;"
                    ),
                ),
            ],
        ),
    ]

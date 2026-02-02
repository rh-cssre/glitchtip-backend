# Remove the `received` column from IssueEvent
#
# The `received` timestamp is now derived from the UUIDv7 id which encodes
# millisecond-precision time. This eliminates 8 bytes per row and removes
# redundant data.
#
# For existing installs: Drops the column if it exists
# For new installs: No-op (column was never created)

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0011_issueevent_issue_id_index"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # Remove from Django model state
                migrations.RemoveField(
                    model_name="issueevent",
                    name="received",
                ),
            ],
            database_operations=[
                # Drop column if it exists (idempotent for new installs)
                migrations.RunSQL(
                    sql="ALTER TABLE issue_events_issueevent DROP COLUMN IF EXISTS received;",
                    reverse_sql="ALTER TABLE issue_events_issueevent ADD COLUMN IF NOT EXISTS received TIMESTAMPTZ;",
                ),
            ],
        ),
    ]

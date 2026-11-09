"""Drop issue_id FK constraints on partitioned tables.

The ON DELETE CASCADE triggers on these constraints must scan every hash
sub-partition to find (or confirm the absence of) child rows.  At 500+
partitions this exceeds statement_timeout even on the maintenance
connection.

Application code in delete_issues_in_batches() already pre-deletes child
rows in the correct order before deleting the parent Issue.

The raw SQL files (create_events_v2.sql, etc.) have been updated to omit
these constraints for new installs.  This migration drops them on
existing databases.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("issue_events", "0016_add_last_release_resolved_in_release"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE issue_events_issueevent DROP CONSTRAINT IF EXISTS issue_events_issueevent_issue_id_fkey;",
                    reverse_sql="ALTER TABLE issue_events_issueevent ADD CONSTRAINT issue_events_issueevent_issue_id_fkey FOREIGN KEY (issue_id) REFERENCES issue_events_issue(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="issueevent",
                    name="issue",
                    field=models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="issue_events.issue",
                    ),
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE issue_events_issuetag DROP CONSTRAINT IF EXISTS issue_events_issuetag_issue_id_fkey;",
                    reverse_sql="ALTER TABLE issue_events_issuetag ADD CONSTRAINT issue_events_issuetag_issue_id_fkey FOREIGN KEY (issue_id) REFERENCES issue_events_issue(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="issuetag",
                    name="issue",
                    field=models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="issue_events.issue",
                    ),
                ),
            ],
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE issue_events_issueaggregate DROP CONSTRAINT IF EXISTS issue_events_issueaggregate_issue_id_fkey;",
                    reverse_sql="ALTER TABLE issue_events_issueaggregate ADD CONSTRAINT issue_events_issueaggregate_issue_id_fkey FOREIGN KEY (issue_id) REFERENCES issue_events_issue(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="issueaggregate",
                    name="issue",
                    field=models.ForeignKey(
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="issue_events.issue",
                    ),
                ),
            ],
        ),
    ]

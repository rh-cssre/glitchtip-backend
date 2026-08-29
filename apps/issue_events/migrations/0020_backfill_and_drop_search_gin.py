"""Drop the old Issue search GIN.

The IssueIndex backfill this migration used to run (canonical columns for every
issue, then the FTS vector for hot ones) is gone: every 6.x install has already
run it, and 7.0 only upgrades from 6.x, so on a fresh install there is nothing
to copy. What remains is the schema half — dropping the GIN that used to back
search on Issue, now that search reads IssueIndex.

``DROP INDEX CONCURRENTLY`` cannot run inside a transaction, hence atomic=False.
"""

from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0019_issueindex"),
    ]

    operations = [
        RemoveIndexConcurrently(
            model_name="issue",
            name="issue_event_search__346c17_gin",
        ),
    ]

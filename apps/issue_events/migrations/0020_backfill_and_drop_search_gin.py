"""Backfill IssueIndex (two phases), then drop the old Issue search GIN.

This is the data-movement step of the Issue "hot-split": the per-event-updated
columns (count/last_seen/status/level/last_release) and the full-text vector are
copied off the wide Issue row onto the hash-partitioned IssueIndex leaf. A
later migration drops those columns from Issue once the application no longer
reads them.

The backfill is split into two phases with very different risk/cost profiles:

Phase 1 -- CANONICAL columns, ALL non-deleted issues. MANDATORY and NOT
abandonable: once Issue's columns are dropped, the leaf holds the only copy, so
a half-done Phase 1 plus a column drop would be a broken issue list. It is also
cheap: fixed-width columns, no GIN, so it is row-bound and runs at ~150k+ rows/s
(seconds even at 10M issues). Keyset-paginated over the id PK with per-batch
autocommit, so it never holds a long transaction and is idempotent/resumable
(``ON CONFLICT DO NOTHING``) -- a killed deploy simply re-runs it.

Phase 2 -- FTS vector, HOT issues only (last_seen within GLITCHTIP_EVENT_HOT_DAYS).
This builds the GIN and is the expensive part: for lexeme-heavy installs it is
GIN-bound, not row-bound. But fts is ABANDONABLE -- search self-heals as issues
receive new events -- so it is bounded by a wall-clock budget
(ISSUE_SEARCH_FTS_BACKFILL_MAX_SECONDS, default 900s). If the budget is hit the
migration stops backfilling fts and completes anyway; the remaining hot issues
become searchable on their next event. This is the "abandon old search data
rather than crash/time out" tradeoff.

Memory is bounded ~flat regardless of vector size: tsvectors large enough to
matter are TOASTed out-of-line, so a batch carries ~18-byte pointers through the
sort and de-TOASTs one row at a time in the streaming INSERT/UPDATE.

Both phases require autocommit (atomic=False) and per-batch commit; the final
``DROP INDEX CONCURRENTLY`` cannot run in a transaction either.
"""

import logging
import time

from django.conf import settings
from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.db import connection, migrations

logger = logging.getLogger(__name__)

# Rows per batch. Each batch is a bounded index-range scan (id for Phase 1,
# last_seen for Phase 2) so memory and lock duration are O(batch), not O(table).
_BATCH = 20000


def backfill_canonical(apps, schema_editor):
    """Phase 1: copy fixed-width canonical columns for every non-deleted issue.

    Mandatory and resumable. Keyset over the id PK; fts_document is left at its
    '' default and filled (for hot issues) by Phase 2.
    """
    with connection.cursor() as cursor:
        after_id = None
        while True:
            params = []
            keyset = ""
            if after_id is not None:
                keyset = "AND i.id > %s "
                params.append(after_id)
            params.append(_BATCH)
            cursor.execute(
                "WITH batch AS ("
                "  SELECT i.id, p.organization_id AS org_id, i.last_release_id,"
                "         i.last_seen, i.count, i.status, i.level"
                "  FROM issue_events_issue i"
                "  JOIN projects_project p ON p.id = i.project_id"
                "  WHERE i.is_deleted = false " + keyset + "  ORDER BY i.id LIMIT %s"
                "), ins AS ("
                "  INSERT INTO issue_events_issueindex"
                "    (issue_id, organization_id, last_release_id, last_seen,"
                "     count, status, level)"
                "  SELECT id, org_id, last_release_id, last_seen, count, status, level"
                "  FROM batch"
                "  ON CONFLICT (issue_id, organization_id) DO NOTHING"
                ") "
                "SELECT id FROM batch ORDER BY id DESC LIMIT 1",
                params,
            )
            row = cursor.fetchone()
            if row is None:  # no more issues
                break
            (after_id,) = row


def backfill_fts_hot(apps, schema_editor):
    """Phase 2: copy the fts vector for hot issues, bounded by a time budget.

    Updates the leaf rows created in Phase 1. Abandonable: if the wall-clock
    budget is exceeded the remaining hot issues keep an empty fts_document and
    repopulate on their next event (self-heal), rather than risking a deploy
    time-out. Copies search_vector verbatim (no to_tsvector recomputation).
    """
    hot_days = settings.GLITCHTIP_EVENT_HOT_DAYS
    budget = getattr(settings, "ISSUE_SEARCH_FTS_BACKFILL_MAX_SECONDS", 900)
    started = time.monotonic()
    with connection.cursor() as cursor:
        cursor.execute("SELECT now() - (%s || ' days')::interval", [hot_days])
        (cutoff,) = cursor.fetchone()

        after_last_seen = None
        after_id = None
        while True:
            if budget and (time.monotonic() - started) > budget:
                logger.warning(
                    "IssueIndex fts backfill hit the %ss budget; "
                    "abandoning the remainder (self-heals on next event).",
                    budget,
                )
                break
            params = [cutoff]
            keyset = ""
            if after_last_seen is not None:
                keyset = "AND (i.last_seen, i.id) > (%s, %s) "
                params += [after_last_seen, after_id]
            params.append(_BATCH)
            cursor.execute(
                "WITH batch AS ("
                "  SELECT i.id, p.organization_id AS org_id, i.search_vector,"
                "         i.last_seen"
                "  FROM issue_events_issue i"
                "  JOIN projects_project p ON p.id = i.project_id"
                "  WHERE i.last_seen >= %s AND i.is_deleted = false"
                "    AND i.search_vector <> ''::tsvector "
                + keyset
                + "  ORDER BY i.last_seen, i.id LIMIT %s"
                "), upd AS ("
                "  UPDATE issue_events_issueindex idx"
                "  SET fts_document = batch.search_vector"
                "  FROM batch"
                "  WHERE idx.issue_id = batch.id"
                "    AND idx.organization_id = batch.org_id"
                ") "
                "SELECT last_seen, id FROM batch ORDER BY last_seen DESC, id DESC "
                "LIMIT 1",
                params,
            )
            row = cursor.fetchone()
            if row is None:  # no more hot issues
                break
            after_last_seen, after_id = row

        # Freshly bulk-loaded partitions have no autoanalyze history; give the
        # planner accurate stats before the search path leans on the new GIN.
        cursor.execute("ANALYZE issue_events_issueindex")


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("issue_events", "0019_issueindex"),
    ]

    operations = [
        migrations.RunPython(code=backfill_canonical, reverse_code=noop),
        migrations.RunPython(code=backfill_fts_hot, reverse_code=noop),
        RemoveIndexConcurrently(
            model_name="issue",
            name="issue_event_search__346c17_gin",
        ),
    ]

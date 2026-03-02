import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Exists, OuterRef
from django.utils.timezone import now

from apps.alerts.models import Notification

from .models import Comment, Issue, IssueAggregate, IssueHash, IssueTag, UserReport

logger = logging.getLogger(__name__)


def cleanup_old_issue_events():
    """
    Archive old issue event partitions to cold storage and delete expired cold data.

    When DuckDB is available:
    - Archive partitions older than GLITCHTIP_EVENT_HOT_DAYS (30d) to S3
    - Delete cold files older than GLITCHTIP_EVENT_RETENTION_DAYS (90d)

    When DuckDB is unavailable:
    - No-op (maintain_partitions handles dropping old partitions at EVENT_RETENTION_DAYS)
    """
    from glitchtip.cold_storage import archive_and_cleanup_partitions

    from .cold_storage import ISSUE_EVENT_EXPORT_COLUMN_TYPES, ISSUE_EVENT_SELECT_SQL

    hot_days = settings.GLITCHTIP_EVENT_HOT_DAYS
    archive_and_cleanup_partitions(
        table_name="issue_events_issueevent",
        hot_days=hot_days,
        column_types=ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        select_sql=ISSUE_EVENT_SELECT_SQL,
        retention_days=settings.GLITCHTIP_EVENT_RETENTION_DAYS,
    )


def cleanup_old_issues():
    """
    Delete Issues whose partitioned data has been dropped.

    maintain_partitions drops old partitions of IssueEvent (daily),
    IssueAggregate (weekly), and IssueTag (weekly) before this runs.
    Because those tables use different partition intervals, an issue's
    events can be dropped before its aggregates/tags — so we must check
    all three partitioned tables are empty before deleting the issue.

    Uses _raw_delete() instead of .delete() to bypass Django's collector,
    which would run unindexed queries against every sub-partition.
    Non-partitioned FK tables (IssueHash, Comment, UserReport) are explicitly
    deleted per batch — Django does not set ON DELETE CASCADE at the DB level.

    NOT EXISTS subqueries short-circuit on the first matching row instead
    of joining all partitions (important for partition-heavy tables).

    If a new FK from a *partitioned* table is added, add an exclude(Exists())
    below. If a new FK from a *non-partitioned* table is added, add it to the
    explicit delete step. Either way, the test will catch the omission.
    """
    from .models import IssueEvent

    days = settings.GLITCHTIP_EVENT_RETENTION_DAYS

    queryset = (
        Issue.objects.filter(last_seen__lt=now() - timedelta(days=days))
        .exclude(Exists(IssueEvent.objects.filter(issue_id=OuterRef("id"))))
        .exclude(Exists(IssueAggregate.objects.filter(issue_id=OuterRef("id"))))
        .exclude(Exists(IssueTag.objects.filter(issue_id=OuterRef("id"))))
        .order_by("id")
    )

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:1000])
        if not batch_ids:
            break
        # Delete from non-partitioned FK tables first (small tables)
        Notification.issues.through.objects.filter(issue_id__in=batch_ids)._raw_delete(
            queryset.db
        )
        IssueHash.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        Comment.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        UserReport.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        # Delete the issues. A new event may arrive between the EXCLUDE check
        # above and this DELETE (TOCTOU race). Catch and skip — the issue will
        # be re-evaluated on the next maintenance run.
        try:
            count = Issue.objects.filter(id__in=batch_ids)._raw_delete(queryset.db)
        except IntegrityError:
            logger.info("Skipped batch due to concurrent FK insert, will retry later")
            continue
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d empty issues", total_deleted)

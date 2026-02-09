import logging
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import Comment, Issue, IssueHash, UserReport

logger = logging.getLogger(__name__)


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

    If a new FK from a *partitioned* table is added, add a =None filter below.
    If a new FK from a *non-partitioned* table is added, add it to the
    explicit delete step. Either way, the test will catch the omission.
    """
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS

    queryset = Issue.objects.filter(
        issueevent=None,
        issueaggregate=None,
        issuetag=None,
        last_seen__lt=now() - timedelta(days=days),
    ).order_by("id")

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:1000])
        if not batch_ids:
            break
        # Delete from non-partitioned FK tables first (small tables)
        IssueHash.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        Comment.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        UserReport.objects.filter(issue_id__in=batch_ids)._raw_delete(queryset.db)
        # Delete the issues
        count = Issue.objects.filter(id__in=batch_ids)._raw_delete(queryset.db)
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d empty issues", total_deleted)

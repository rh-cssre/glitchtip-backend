import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError
from django.db.models import QuerySet
from django.utils.timezone import now

from apps.alerts.models import Notification

from .models import Comment, Issue, IssueHash, UserReport

logger = logging.getLogger(__name__)


async def delete_events_in_batches(
    queryset: QuerySet, batch_size: int = 1000, db_alias: str = "default"
) -> int:
    """
    Bulk-delete rows from a partitioned event table (IssueEvent, LogEvent, etc.)
    in fixed-size batches to avoid statement timeouts.

    The caller should pre-filter the queryset with organization_id for
    partition pruning.

    Returns the total number of rows deleted.
    """
    ordered_qs = queryset.using(db_alias).order_by("id")
    model = queryset.model

    total_deleted = 0
    while True:
        batch_ids = await sync_to_async(list)(
            ordered_qs.values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break
        count = await sync_to_async(
            model.objects.filter(id__in=batch_ids)._raw_delete
        )(db_alias)
        total_deleted += count

    return total_deleted


async def delete_issues_in_batches(
    queryset: QuerySet[Issue], batch_size: int = 1000, db_alias: str = "default"
) -> int:
    """
    Bulk-delete Issues and their non-partitioned FK dependents.

    Uses _raw_delete() to bypass Django's collector, which would run
    unindexed queries against every sub-partition of partitioned tables.

    Non-partitioned FK tables (IssueHash, Comment, UserReport,
    Notification.issues M2M) are explicitly deleted per batch — Django
    does not set ON DELETE CASCADE at the DB level.

    If a new FK from a *non-partitioned* table is added to Issue, add it
    here. The test_cleanup_old_issues test will catch the omission.

    Returns the total number of Issues deleted.
    """
    ordered_qs = queryset.using(db_alias).order_by("id")

    total_deleted = 0
    while True:
        batch_ids = await sync_to_async(list)(
            ordered_qs.values_list("id", flat=True)[:batch_size]
        )
        if not batch_ids:
            break
        # Delete from non-partitioned FK tables first
        await sync_to_async(
            Notification.issues.through.objects.filter(
                issue_id__in=batch_ids
            )._raw_delete
        )(db_alias)
        await sync_to_async(
            IssueHash.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        await sync_to_async(
            Comment.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        await sync_to_async(
            UserReport.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        # A new event may arrive between the SELECT above and this DELETE
        # (TOCTOU race). Catch and skip — the caller can retry later.
        try:
            count = await sync_to_async(
                Issue.objects.filter(id__in=batch_ids)._raw_delete
            )(db_alias)
        except IntegrityError:
            logger.info(
                "Skipped batch due to concurrent FK insert, will retry later"
            )
            continue
        total_deleted += count

    return total_deleted


async def cleanup_old_issue_events():
    """
    Archive old issue event partitions to cold storage and delete expired cold data.

    When DuckDB is available:
    - Archive partitions older than GLITCHTIP_EVENT_HOT_DAYS (30d) to S3
    - Delete cold files older than GLITCHTIP_EVENT_RETENTION_DAYS (90d)

    When DuckDB is unavailable:
    - No-op (maintain_partitions handles dropping old partitions at EVENT_RETENTION_DAYS)
    """
    from glitchtip.cold_storage import archive_and_cleanup_partitions

    from .cold_storage import (
        DICTIONARY_COLUMNS,
        ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        ISSUE_EVENT_SELECT_SQL,
    )

    hot_days = settings.GLITCHTIP_EVENT_HOT_DAYS
    await sync_to_async(archive_and_cleanup_partitions)(
        table_name="issue_events_issueevent",
        hot_days=hot_days,
        column_types=ISSUE_EVENT_EXPORT_COLUMN_TYPES,
        select_sql=ISSUE_EVENT_SELECT_SQL,
        retention_days=settings.GLITCHTIP_EVENT_RETENTION_DAYS,
        db_alias=settings.MAINTENANCE_DATABASE_ALIAS,
        dictionary_columns=DICTIONARY_COLUMNS,
    )


async def cleanup_old_issues():
    """
    Delete Issues whose partitioned data has been dropped.

    maintain_partitions drops old partitions of IssueEvent (daily),
    IssueAggregate (weekly), and IssueTag (weekly) before this runs.
    Rather than running expensive NOT EXISTS subqueries against every
    partition, we rely on maintain_partitions having already dropped
    old partitions and add a 7-day buffer beyond retention to ensure
    weekly partitions (IssueAggregate/IssueTag) are fully dropped.
    """
    days = settings.GLITCHTIP_EVENT_RETENTION_DAYS
    # 7-day buffer ensures weekly partitions (IssueAggregate, IssueTag)
    # are fully dropped before we delete the parent Issue.
    buffer_days = 7
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS

    queryset = Issue.objects.filter(
        last_seen__lt=now() - timedelta(days=days + buffer_days)
    )

    total_deleted = await delete_issues_in_batches(queryset, db_alias=db_alias)
    if total_deleted:
        logger.info("Deleted %d empty issues", total_deleted)

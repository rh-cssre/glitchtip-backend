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


async def raw_delete_in_batches(
    queryset: QuerySet, batch_size: int = 1000, db_alias: str = "default"
) -> int:
    """
    Bulk-delete rows from a partitioned table in fixed-size batches to
    avoid exhausting the PostgreSQL shared lock table.

    The queryset must be for a model with an ``id`` field.  Pre-filter
    with organization_id when possible for partition pruning.

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
        count = await sync_to_async(model.objects.filter(id__in=batch_ids)._raw_delete)(
            db_alias
        )
        total_deleted += count

    return total_deleted


async def delete_issues_in_batches(
    queryset: QuerySet[Issue], batch_size: int = 1000, db_alias: str = "default"
) -> int:
    """
    Bulk-delete Issues and their FK dependents.

    Uses _raw_delete() to bypass Django's collector, which would run
    unindexed queries against every sub-partition of partitioned tables.

    Partitioned FK tables (IssueEvent, IssueAggregate, IssueTag) have
    DB-level ON DELETE CASCADE, but that cascade acquires locks across
    all partitions.  We pre-delete them per batch so the Issue _raw_delete
    triggers no cascading locks.

    IssueIndex is partitioned with NO DB-level FK, so nothing
    cascades to it at all — it must be explicitly deleted here or its
    rows orphan permanently.

    Non-partitioned FK tables (IssueHash, Comment, UserReport,
    Notification.issues M2M) are also explicitly deleted per batch.

    If a new FK to Issue is added from any table without a DB-level
    CASCADE to Issue (every partitioned table, plus non-partitioned
    tables), add it here. test_cleanup_old_issues catches non-partitioned
    omissions; partitioned-without-FK tables (like IssueIndex) fail
    silently, so test_cleanup_deletes_index guards that case.

    Returns the total number of Issues deleted.
    """
    from .models import IssueAggregate, IssueEvent, IssueIndex, IssueTag

    ordered_qs = queryset.using(db_alias).order_by("id")

    total_deleted = 0
    batch_ids = await sync_to_async(list)(
        ordered_qs.values_list("id", flat=True)[:batch_size]
    )
    while batch_ids:
        # Delete from partitioned FK tables first — these have DB-level
        # CASCADE which would lock every partition when the Issue is deleted.
        await raw_delete_in_batches(
            IssueEvent.objects.filter(issue_id__in=batch_ids), db_alias=db_alias
        )
        for model in [IssueAggregate, IssueTag]:
            await sync_to_async(
                model.objects.filter(issue_id__in=batch_ids)._raw_delete
            )(db_alias)
        # IssueIndex is partitioned but, unlike the tables above, has
        # no DB-level FK/CASCADE (partitioned tables can't be FK targets).
        # The Issue _raw_delete will therefore succeed without touching it,
        # so this explicit delete is mandatory for correctness — skipping
        # it silently orphans search rows and re-grows the GIN index this
        # table exists to shrink.
        await sync_to_async(
            IssueIndex.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        # Delete from non-partitioned FK tables
        await sync_to_async(
            Notification.issues.through.objects.filter(
                issue_id__in=batch_ids
            )._raw_delete
        )(db_alias)
        await sync_to_async(
            IssueHash.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        await sync_to_async(Comment.objects.filter(issue_id__in=batch_ids)._raw_delete)(
            db_alias
        )
        await sync_to_async(
            UserReport.objects.filter(issue_id__in=batch_ids)._raw_delete
        )(db_alias)
        # A new event may arrive between the SELECT above and this DELETE
        # (TOCTOU race). Stop and let the next maintenance run retry —
        # continuing would re-select the same undeletable batch forever.
        try:
            count = await sync_to_async(
                Issue.objects.filter(id__in=batch_ids)._raw_delete
            )(db_alias)
        except IntegrityError:
            logger.info(
                "Skipped issue batch due to concurrent FK insert, will retry later"
            )
            break
        total_deleted += count
        batch_ids = await sync_to_async(list)(
            ordered_qs.values_list("id", flat=True)[:batch_size]
        )

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
        MAX_ROW_GROUP_SIZE,
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
        max_row_group_size=MAX_ROW_GROUP_SIZE,
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

    # last_seen lives on the IssueIndex leaf.
    queryset = Issue.objects.filter(
        index__last_seen__lt=now() - timedelta(days=days + buffer_days)
    )

    total_deleted = await delete_issues_in_batches(queryset, db_alias=db_alias)
    if total_deleted:
        logger.info("Deleted %d empty issues", total_deleted)

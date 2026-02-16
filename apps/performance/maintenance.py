import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils.timezone import now

from .models import TransactionEvent, TransactionGroup, TransactionGroupAggregate

logger = logging.getLogger(__name__)


def cleanup_old_transaction_events():
    """
    Delete TransactionGroups whose partitioned data has been dropped.

    maintain_partitions drops old partitions of TransactionEvent (daily) and
    TransactionGroupAggregate (weekly) before this runs. Because those tables
    use different partition intervals, a group's events can be dropped before
    its aggregates — so we must check both are gone before deleting the group.

    Optimizations:
    - created__lt filter skips groups too new to have lost all partitions.
    - NOT EXISTS subqueries short-circuit on the first matching row instead
      of joining all partitions (important for partition-heavy tables).
    - ID collection + batch delete keeps CASCADE FK work per statement small.
    """
    cutoff = now() - timedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS)
    queryset = (
        TransactionGroup.objects.filter(created__lt=cutoff)
        .exclude(Exists(TransactionEvent.objects.filter(group_id=OuterRef("id"))))
        .exclude(
            Exists(TransactionGroupAggregate.objects.filter(group_id=OuterRef("id")))
        )
        .order_by("id")
    )

    total_deleted = 0
    while True:
        batch_ids = list(queryset.values_list("id", flat=True)[:500])
        if not batch_ids:
            break
        count = TransactionGroup.objects.filter(id__in=batch_ids)._raw_delete(
            queryset.db
        )
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d empty transaction groups", total_deleted)

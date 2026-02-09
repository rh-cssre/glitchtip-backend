import logging

from .models import TransactionGroup

logger = logging.getLogger(__name__)


def cleanup_old_transaction_events():
    """
    Delete TransactionGroups whose partitioned data has been dropped.

    maintain_partitions drops old partitions of TransactionEvent (daily) and
    TransactionGroupAggregate (weekly) before this runs. Because those tables
    use different partition intervals, a group's events can be dropped before
    its aggregates — so we must check both are gone before deleting the group.

    Uses _raw_delete() instead of .delete() to bypass Django's collector, which
    would run unindexed queries against every sub-partition of both tables.
    If a new FK referencing TransactionGroup is added, the DB will raise
    IntegrityError here — add the corresponding filter to the queryset below.
    """
    queryset = TransactionGroup.objects.filter(
        transactionevent=None,
        transactiongroupaggregate=None,
    ).order_by("id")

    total_deleted = 0
    while True:
        try:
            empty_group_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            count = queryset.filter(id__lte=empty_group_delimiter)._raw_delete(
                queryset.db
            )
            total_deleted += count
        except TransactionGroup.DoesNotExist:
            break

    total_deleted += queryset._raw_delete(queryset.db)
    if total_deleted:
        logger.info("Deleted %d empty transaction groups", total_deleted)

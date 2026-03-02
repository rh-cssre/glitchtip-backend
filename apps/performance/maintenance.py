import logging
from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from glitchtip.cold_storage import cleanup_all_cold_storage

from .cold_storage import TABLE_NAME
from .models import TransactionGroup

logger = logging.getLogger(__name__)


def cleanup_old_transaction_events():
    """
    Delete old TransactionGroups and clean up cold storage.

    Groups are deleted when last_seen < retention cutoff.
    Cold storage Parquet files older than retention are also cleaned.
    """
    cutoff = now() - timedelta(days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS)

    # Delete old groups in batches
    queryset = TransactionGroup.objects.filter(last_seen__lt=cutoff).order_by("id")

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
        logger.info("Deleted %d old transaction groups", total_deleted)

    # Clean up cold storage files
    cleanup_all_cold_storage(
        retention_days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS,
        table_name=TABLE_NAME,
    )

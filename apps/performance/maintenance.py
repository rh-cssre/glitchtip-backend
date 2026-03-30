import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.utils.timezone import now

from glitchtip.cold_storage import cleanup_all_cold_storage

from .cold_storage import TABLE_NAME
from .models import TransactionGroup

logger = logging.getLogger(__name__)


async def cleanup_old_transaction_events():
    """
    Delete old TransactionGroups and clean up cold storage.

    Groups are deleted when last_seen < retention cutoff.
    Cold storage Parquet files older than retention are also cleaned.
    """
    cutoff = now() - timedelta(days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS)
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS

    # Delete old groups in batches
    queryset = (
        TransactionGroup.objects.using(db_alias)
        .filter(last_seen__lt=cutoff)
        .order_by("id")
    )

    total_deleted = 0
    while True:
        batch_ids = await sync_to_async(list)(
            queryset.values_list("id", flat=True)[:500]
        )
        if not batch_ids:
            break
        count = await sync_to_async(
            TransactionGroup.objects.filter(id__in=batch_ids)._raw_delete
        )(db_alias)
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old transaction groups", total_deleted)

    # Clean up cold storage files
    await sync_to_async(cleanup_all_cold_storage)(
        retention_days=settings.GLITCHTIP_TRANSACTION_RETENTION_DAYS,
        table_name=TABLE_NAME,
    )

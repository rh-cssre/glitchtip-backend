import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils.timezone import now

from .models import File, FileBlob

logger = logging.getLogger(__name__)


def cleanup_old_files():
    """
    Delete old FileBlobs and their storage files.

    A FileBlob is deleted when:
    - It was created before the retention cutoff, AND
    - No recent File record references it (source maps are resolved at ingest
      time via debug ID or release — once symbolicated, the blob is not needed)

    Batches deletes to limit memory and transaction size.
    """
    days_ago = now() - timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)

    queryset = FileBlob.objects.filter(created__lt=days_ago).exclude(
        Exists(File.objects.filter(blob_id=OuterRef("id"), created__gte=days_ago))
    )

    total_deleted = 0
    while True:
        file_blobs = list(queryset.only("id", "blob")[:1000])
        if not file_blobs:
            break
        ids = []
        for file_blob in file_blobs:
            ids.append(file_blob.id)
            file_blob.blob.delete()  # Delete from object storage
        count, _ = FileBlob.objects.filter(id__in=ids).delete()
        total_deleted += count

    if total_deleted:
        logger.info("Deleted %d old file blobs", total_deleted)

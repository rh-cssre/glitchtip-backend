import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Exists, OuterRef
from django.utils.timezone import now

from .models import File, FileBlob

logger = logging.getLogger(__name__)


MAX_DELETIONS_PER_RUN = 10_000


async def _delete_file_blobs(queryset, label):
    total_deleted = 0
    while total_deleted < MAX_DELETIONS_PER_RUN:
        file_blobs = [fb async for fb in queryset.only("id", "blob")[:1000].aiterator()]
        if not file_blobs:
            break
        ids = [file_blob.id for file_blob in file_blobs]
        # Delete rows before storage, filtering through the original queryset
        # so its conditions are re-evaluated at delete time. Uploads dedupe on
        # checksum with get_or_create, so a blob fetched as orphaned can gain
        # a File reference before this delete runs; re-checking narrows that
        # window, and deleting rows first means a skipped blob keeps both its
        # row and its storage — a crash here can only leak an unreferenced
        # storage object, never leave a File whose bytes are gone.
        _, per_model = await queryset.filter(id__in=ids).adelete()
        deleted_blobs = per_model.get(FileBlob._meta.label, 0)
        if deleted_blobs == len(ids):
            survivors = frozenset()
        else:
            survivors = {
                pk
                async for pk in FileBlob.objects.using(
                    settings.MAINTENANCE_DATABASE_ALIAS
                )
                .filter(id__in=ids)
                .values_list("id", flat=True)
                .aiterator()
            }
        for file_blob in file_blobs:
            if file_blob.id in survivors:
                continue
            try:
                # django-storages has no async API, so the storage delete
                # must hop threads. save=False skips an UPDATE on the
                # already-deleted row.
                await sync_to_async(file_blob.blob.delete)(save=False)
            except Exception:
                logger.warning(
                    "Failed to delete storage for FileBlob %d",
                    file_blob.id,
                    exc_info=True,
                )
        total_deleted += deleted_blobs
    if total_deleted:
        logger.info("Deleted %d %s file blobs", total_deleted, label)


async def cleanup_old_files():
    """
    Delete stale and orphaned FileBlobs and their storage files.

    Two passes:
    1. Retention: delete FileBlobs created before the retention cutoff that
       have no recent File referencing them.
    2. Orphans: delete FileBlobs with zero File references that are older
       than 24 hours (grace period to protect in-flight uploads). Orphans
       arise from multi-chunk assembly, source-map re-uploads, and
       abandoned uploads.
    """
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS

    days_ago = now() - timedelta(days=settings.GLITCHTIP_FILE_RETENTION_DAYS)
    retention_qs = (
        FileBlob.objects.using(db_alias)
        .filter(created__lt=days_ago)
        .exclude(
            Exists(File.objects.filter(blob_id=OuterRef("id"), created__gte=days_ago))
        )
    )
    await _delete_file_blobs(retention_qs, "old")

    cutoff = now() - timedelta(hours=24)
    orphan_qs = (
        FileBlob.objects.using(db_alias)
        .filter(created__lt=cutoff)
        .exclude(Exists(File.objects.filter(blob_id=OuterRef("id"))))
    )
    await _delete_file_blobs(orphan_qs, "orphaned")

import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import Q

from .models import CacheKind, SymbolicationCache
from .store import format_tag

logger = logging.getLogger(__name__)

MAX_DELETIONS_PER_RUN = 10_000


def _delete_stored(name: str) -> bool:
    try:
        default_storage.delete(name)
    except Exception:
        logger.warning("Failed to delete symbolication cache %s", name, exc_info=True)
        return False
    return True


async def cleanup_symbolication_caches():
    """Delete caches that can no longer be used, and their stored bytes.

    A cache goes when a source it was built from is deleted (the FileBlob
    foreign key is set NULL: retention, project deletion and orphan cleanup
    all end there), or when it was built in a format this gt_rust no longer
    reads (an upgrade; the next event rebuilds it lazily).
    """
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    unusable = (
        Q(source_blob__isnull=True)
        | Q(kind=CacheKind.SOURCEMAP, secondary_blob__isnull=True)
        | Q(
            *[Q(kind=kind) & ~Q(format_tag=format_tag(kind)) for kind in CacheKind],
            _connector=Q.OR,
        )
    )
    queryset = SymbolicationCache.objects.using(db_alias).filter(unusable)
    deleted = 0
    while deleted < MAX_DELETIONS_PER_RUN:
        rows = [row async for row in queryset.only("id", "storage_name")[:1000]]
        if not rows:
            break
        removable = []
        for row in rows:
            # django-storages has no async API.
            if not row.storage_name or await sync_to_async(_delete_stored)(
                row.storage_name
            ):
                removable.append(row.id)
        if not removable:
            break
        count, _ = (
            await SymbolicationCache.objects.using(db_alias)
            .filter(id__in=removable)
            .adelete()
        )
        deleted += count
    if deleted:
        logger.info("Deleted %d symbolication caches", deleted)

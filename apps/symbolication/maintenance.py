import logging
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import CacheKind, CacheState, SymbolicationCache
from .sources import load_lane
from .store import _delete_stored, format_tag

logger = logging.getLogger(__name__)

PAGE_SIZE = 500
# Maintenance runs among other steps; stop after this long and continue on
# the next run.
TIME_BUDGET_SECONDS = 60
# A row in another cache format is only deleted once it has been left alone
# this long. During a rolling deploy, pods on the old and the new gt_rust
# both run, and whichever runs maintenance must not delete the other's
# caches.
STALE_FORMAT_GRACE = timedelta(days=1)


def _unusable(now) -> Q:
    stale_format = Q(
        *[Q(kind=kind) & ~Q(format_tag=format_tag(kind)) for kind in CacheKind],
        _connector=Q.OR,
    )
    return (
        Q(source_blob__isnull=True)
        | Q(kind=CacheKind.SOURCEMAP, secondary_blob__isnull=True)
        | (stale_format & Q(updated__lt=now - STALE_FORMAT_GRACE))
        # Failed builds are forgotten eventually, so one that failed for a
        # cause since removed (a raised cap, an outage) is tried again.
        | Q(
            state=CacheState.FAILED,
            updated__lt=now
            - timedelta(days=settings.GLITCHTIP_SYMBOLICATION_FAILED_EXPIRY_DAYS),
        )
    )


def _delete_all_stored(names: list[str]):
    for name in names:
        _delete_stored(name)


async def cleanup_symbolication_caches():
    """Delete caches that can no longer be used, and their stored bytes.

    A cache goes when a source it was built from is deleted (the database
    sets the FileBlob foreign key NULL: retention, project deletion and
    orphan cleanup all end there), when it was built in a format this
    gt_rust no longer reads (an upgrade; the next event rebuilds it lazily),
    or when its build failed long enough ago. A row deleted while its build
    is still running is harmless: the build finds its row gone and deletes
    what it stored.
    """
    db_alias = settings.MAINTENANCE_DATABASE_ALIAS
    queryset = SymbolicationCache.objects.using(db_alias).filter(
        _unusable(timezone.now())
    )
    started = time.monotonic()
    last_id = 0
    deleted = 0
    while time.monotonic() - started < TIME_BUDGET_SECONDS:
        rows = [
            row
            async for row in queryset.filter(id__gt=last_id)
            .order_by("id")
            .only("id", "storage_name")[:PAGE_SIZE]
        ]
        if not rows:
            break
        last_id = rows[-1].id
        count, _ = (
            await SymbolicationCache.objects.using(db_alias)
            .filter(id__in=[row.id for row in rows])
            .adelete()
        )
        deleted += count
        # Rows first, then their bytes: a failure between the two leaks only
        # unreferenced storage, never a row pointing at missing bytes.
        await load_lane.run(
            _delete_all_stored, [row.storage_name for row in rows if row.storage_name]
        )
    if deleted:
        logger.info("Deleted %d symbolication caches", deleted)

"""Building symbolication caches, on the dedicated ``symbolication`` queue."""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone
from gt_rust.symbolic import SymbolicError

from .models import CacheKind, CacheState, SymbolicationCache
from .sources import blob_path
from .store import LOADERS, MAX_ATTEMPTS, STALE_BUILD, _notify, storage_name

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _build_executor() -> ThreadPoolExecutor:
    """A small pool for builds only, created on first use, so builds never
    compete with the loop's default executor."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=max(settings.GLITCHTIP_SYMBOLICATION_BUILD_CONCURRENCY, 1),
                thread_name_prefix="symbolication-build",
            )
        return _executor


def _build(row: SymbolicationCache) -> tuple[str, int]:
    """Build and store the cache for ``row``. Runs on the build executor."""
    loader = LOADERS[row.kind]
    # The raw SHA-1 of the source, recorded in the cache header.
    try:
        digest = bytes.fromhex(row.source_blob.checksum)
    except ValueError:
        digest = None
    with blob_path(row.source_blob) as source:
        if row.kind == CacheKind.SOURCEMAP:
            with blob_path(row.secondary_blob) as sourcemap:
                data = loader.build(source, sourcemap, source_digest=digest)
        elif row.kind == CacheKind.SYMCACHE:
            _, debug_id, arch = row.cache_key.split(":", 2)
            data = loader.build(
                source,
                debug_id=debug_id or None,
                arch=arch or None,
                source_digest=digest,
            )
        else:
            data = loader.build(
                source,
                source_digest=digest,
                max_mapping_bytes=settings.GLITCHTIP_PROGUARD_MAX_MAPPING_MB
                * 1024
                * 1024,
            )
    name = storage_name(row.kind, row.cache_key)
    if default_storage.exists(name):
        default_storage.delete(name)
    stored = default_storage.save(name, ContentFile(data))
    return stored, len(data)


@task(queue_name="symbolication")
async def build_cache(cache_id: int):
    """Build one cache. Single-flight: the row is claimed with a conditional
    UPDATE, so of any number of queued builds for it (on any pod) one runs.
    """
    now = timezone.now()
    claimed = await SymbolicationCache.objects.filter(
        Q(state=CacheState.PENDING)
        | Q(state=CacheState.BUILDING, updated__lt=now - STALE_BUILD),
        id=cache_id,
    ).aupdate(state=CacheState.BUILDING, updated=now, attempts=F("attempts") + 1)
    if not claimed:
        return
    row = (
        await SymbolicationCache.objects.select_related("source_blob", "secondary_blob")
        .filter(id=cache_id)
        .afirst()
    )
    if row is None:
        return
    if row.source_blob is None or (
        row.kind == CacheKind.SOURCEMAP and row.secondary_blob is None
    ):
        await _finish(row, CacheState.FAILED, error="source deleted")
        return
    try:
        stored, size = await asyncio.get_running_loop().run_in_executor(
            _build_executor(), _build, row
        )
    except SymbolicError as err:
        # The input itself cannot be turned into a cache (malformed, or over
        # the size cap): do not retry it with this format.
        logger.info("Symbolication cache %s cannot be built: %s", cache_id, err)
        await _finish(
            row, CacheState.FAILED, error=str(err)[:1000], attempts=MAX_ATTEMPTS
        )
        return
    except Exception as err:
        logger.warning("Symbolication cache %s build failed", cache_id, exc_info=True)
        await _finish(row, CacheState.FAILED, error=str(err)[:1000])
        return
    _notify("build")
    await _finish(row, CacheState.READY, storage_name=stored, size=size)


async def _finish(row: SymbolicationCache, state: CacheState, **fields):
    await SymbolicationCache.objects.filter(
        id=row.id, state=CacheState.BUILDING
    ).aupdate(state=state, updated=timezone.now(), **fields)

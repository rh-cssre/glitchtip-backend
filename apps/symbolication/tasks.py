"""Building symbolication caches, on the dedicated ``symbolication`` queue."""

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.files.base import ContentFile, File
from django.core.files.storage import default_storage
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone
from gt_rust.symbolic import CacheTooLargeError, SymbolicError

from .models import CacheKind, CacheState, SymbolicationCache
from .sources import blob_input
from .store import (
    LOADERS,
    MAX_ATTEMPTS,
    STALE_BUILD,
    _notify,
    source_digest,
    storage_name,
)

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


def _build_args(row: SymbolicationCache, source, sourcemap) -> tuple[tuple, dict]:
    """The arguments to the loader's build/build_to after the destination."""
    digest = source_digest(row.source_blob.checksum)
    if row.kind == CacheKind.SOURCEMAP:
        return (source, sourcemap), {"source_digest": digest}
    if row.kind == CacheKind.SYMCACHE:
        _, debug_id, arch = row.cache_key.split(":", 2)
        return (source,), {
            "debug_id": debug_id or None,
            "arch": arch or None,
            "source_digest": digest,
        }
    return (source,), {
        "source_digest": digest,
        "max_mapping_bytes": settings.GLITCHTIP_PROGUARD_MAX_MAPPING_MB * 1024 * 1024,
    }


def _store(name: str, content) -> str:
    # Replace a cache only by delete + save, never by rewriting the file in
    # place: with local file storage other processes may have the old file
    # memory-mapped (store._load_from_tiers). Unlinking leaves their maps
    # valid; truncating or overwriting would not.
    if default_storage.exists(name):
        default_storage.delete(name)
    return default_storage.save(name, content)


def _build(row: SymbolicationCache) -> tuple[str, int]:
    """Build and store the cache for ``row``. Runs on the build executor.

    The cache is written to a temporary file by gt_rust (build_to) and
    streamed to storage from there, so a large cache is never held as one
    Python bytes object; without a writable temporary directory it is built
    in memory instead.
    """
    loader = LOADERS[row.kind]
    name = storage_name(row.kind, row.cache_key)
    with contextlib.ExitStack() as inputs:
        source = inputs.enter_context(blob_input(row.source_blob))
        sourcemap = None
        if row.kind == CacheKind.SOURCEMAP:
            sourcemap = inputs.enter_context(blob_input(row.secondary_blob))
        args, kwargs = _build_args(row, source, sourcemap)
        try:
            scratch = tempfile.TemporaryDirectory(prefix="gt-symbol-cache-")
        except OSError:
            data = loader.build(*args, **kwargs)
            return _store(name, ContentFile(data)), len(data)
        with scratch as directory:
            built = os.path.join(directory, "cache")
            size = loader.build_to(built, *args, **kwargs)
            with open(built, "rb") as cache_file:
                return _store(name, File(cache_file, name=name)), size


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
    except CacheTooLargeError as err:
        # Deterministic for this input: never retried. Events that need it
        # are stored without ProGuard deobfuscation.
        logger.warning(
            "ProGuard mapping for symbolication cache %s is over "
            "GLITCHTIP_PROGUARD_MAX_MAPPING_MB; events using it stay "
            "obfuscated: %s",
            cache_id,
            err,
        )
        await _finish(
            row, CacheState.FAILED, error=str(err)[:1000], attempts=MAX_ATTEMPTS
        )
        return
    except SymbolicError as err:
        # The input itself cannot be turned into a cache (malformed): do not
        # retry it with this format.
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

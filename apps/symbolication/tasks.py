"""Building symbolication caches, on the dedicated ``symbolication`` queue."""

import asyncio
import contextlib
import logging
import os
import secrets
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

from glitchtip.memory_trim import malloc_trim

from .models import CacheKind, CacheState, SymbolicationCache
from .sources import blob_input, load_lane
from .store import (
    BUILD_HEARTBEAT,
    LOADERS,
    MAX_ATTEMPTS,
    STALE_BUILD,
    _delete_stored,
    _notify,
    source_digest,
    storage_name,
)

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = BUILD_HEARTBEAT.total_seconds()

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


class InputTooLarge(Exception):
    """A build input is over its size cap."""


def _build_executor() -> ThreadPoolExecutor:
    """A small pool for builds only, created on first use, so builds never
    compete with the loop's default executor."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=settings.GLITCHTIP_SYMBOLICATION_BUILD_CONCURRENCY,
                thread_name_prefix="symbolication-build",
            )
        return _executor


def _check_input_size(row: SymbolicationCache):
    """Refuse inputs whose build would need too much memory."""
    mb = 1024 * 1024
    if row.kind == CacheKind.SOURCEMAP:
        size = (row.source_blob.size or 0) + (row.secondary_blob.size or 0)
        cap = settings.GLITCHTIP_SYMBOLICATION_MAX_SOURCEMAP_MB * mb
    elif row.kind == CacheKind.SYMCACHE:
        size = row.source_blob.size or 0
        cap = settings.GLITCHTIP_SYMBOLICATION_MAX_SYMCACHE_MB * mb
    else:
        return  # gt_rust applies GLITCHTIP_PROGUARD_MAX_MAPPING_MB itself
    if size > cap:
        raise InputTooLarge(f"input is {size} bytes, cap is {cap}")


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


def _build(row: SymbolicationCache, nonce: str) -> tuple[str, int]:
    """Build and store the cache for ``row``. Runs on the build executor.

    The cache is written to a temporary file by gt_rust (build_to) and
    streamed to storage from there, so a large cache is never held as one
    Python bytes object; without a writable temporary directory it is built
    in memory instead. Each build is stored under a fresh name, never over
    an existing file: with local file storage other processes may have the
    previous build memory-mapped, and only unlinking (not truncating or
    rewriting) keeps their maps valid.
    """
    try:
        loader = LOADERS[row.kind]
        name = storage_name(row.kind, row.cache_key, nonce)
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
                return default_storage.save(name, ContentFile(data)), len(data)
            with scratch as directory:
                built = os.path.join(directory, "cache")
                size = loader.build_to(built, *args, **kwargs)
                with open(built, "rb") as cache_file:
                    return default_storage.save(name, File(cache_file, name=name)), size
    finally:
        # A build's peak can be hundreds of MB, freed only to this thread's
        # malloc arena; hand it back to the OS now, not at the next trim.
        malloc_trim()


async def _finish(row: SymbolicationCache, state: CacheState, **fields) -> int:
    return await SymbolicationCache.objects.filter(
        id=row.id, state=CacheState.BUILDING
    ).aupdate(state=state, updated=timezone.now(), **fields)


async def _run_build(row: SymbolicationCache, nonce: str):
    """Run the build on the build executor, refreshing the row's claim while
    it runs."""
    future = asyncio.wrap_future(_build_executor().submit(_build, row, nonce))
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(future), HEARTBEAT_SECONDS)
        except TimeoutError:
            await SymbolicationCache.objects.filter(
                id=row.id, state=CacheState.BUILDING
            ).aupdate(updated=timezone.now())


@task(queue_name="symbolication")
async def build_cache(cache_id: int, organization_id: int | None = None):
    """Build one cache. Single-flight: the row is claimed with a conditional
    UPDATE, so of any number of queued builds for it (on any pod) one runs.
    ``organization_id`` is the organization whose event asked for it.
    """
    now = timezone.now()
    stale = now - STALE_BUILD
    claimed = await SymbolicationCache.objects.filter(
        Q(state=CacheState.PENDING)
        | Q(state=CacheState.BUILDING, updated__lt=stale, attempts__lt=MAX_ATTEMPTS),
        id=cache_id,
    ).aupdate(state=CacheState.BUILDING, updated=now, attempts=F("attempts") + 1)
    if not claimed:
        # A claim that went stale on its last attempt: give up on it.
        if await SymbolicationCache.objects.filter(
            id=cache_id,
            state=CacheState.BUILDING,
            updated__lt=stale,
            attempts__gte=MAX_ATTEMPTS,
        ).aupdate(state=CacheState.FAILED, error="build did not finish", updated=now):
            logger.warning(
                "Symbolication cache %s build never finished; giving up.", cache_id
            )
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
        _check_input_size(row)
        stored, size = await _run_build(row, secrets.token_hex(8))
    except asyncio.CancelledError:
        # The worker is shutting down: leave the row for the next build.
        await asyncio.shield(
            SymbolicationCache.objects.filter(
                id=row.id, state=CacheState.BUILDING
            ).aupdate(state=CacheState.PENDING, updated=timezone.now())
        )
        raise
    except OSError as err:
        # Storage or disk trouble reading the input or writing the cache:
        # not the input's fault, so try again later.
        await _fail_transient(row, err)
        return
    except (CacheTooLargeError, InputTooLarge) as err:
        logger.warning(
            "Symbolication cache %s input is over its size cap; events using "
            "it are stored unsymbolicated: %s",
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
        # Anything else, object storage client errors included: retry later.
        await _fail_transient(row, err)
        return

    _notify("build")
    if not await _finish(row, CacheState.READY, storage_name=stored, size=size):
        # The row moved on while this build ran (deleted by maintenance, or
        # taken over): its bytes would be orphaned.
        await load_lane.run(_delete_stored, stored)
    elif row.storage_name and row.storage_name != stored:
        # A rebuild: the previous build's bytes are no longer referenced.
        await load_lane.run(_delete_stored, row.storage_name)


async def _fail_transient(row: SymbolicationCache, err: Exception):
    logger.warning(
        "Symbolication cache %s build failed (%s); it will be retried.",
        row.id,
        type(err).__name__,
    )
    await _finish(row, CacheState.FAILED, error=type(err).__name__)

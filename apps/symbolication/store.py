"""Loaded symbolication caches, and the tiers they come from.

An event asks for the cache of one artifact with ``get_cache``. The lookup
goes, cheapest first:

1. the in-process RAM tier (an LRU of loaded caches, bounded in bytes, that
   drains to empty when idle);
2. the optional node-local disk tier (``GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR``),
   whose files are memory-mapped;
3. the cache stored in the default file storage, the source of truth;
4. a build, queued on the ``symbolication`` task queue. The event waits for
   it up to ``GLITCHTIP_SYMBOLICATION_BUILD_WAIT_SECONDS`` and otherwise goes
   on without it. Events never parse an upload themselves.
"""

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.utils import timezone
from gt_rust.symbolic import (
    ProguardCache,
    SourceMapCache,
    SymbolicError,
    SymCache,
    cache_format_tag,
)

from .models import CacheKind, CacheState, SymbolicationCache

logger = logging.getLogger(__name__)

LOADERS = {
    CacheKind.SOURCEMAP: SourceMapCache,
    CacheKind.SYMCACHE: SymCache,
    CacheKind.PROGUARD: ProguardCache,
}
# Next to the uploaded files (FileBlob's upload_to), so a deployment that
# keeps uploads/ on a volume keeps its caches too.
STORAGE_PREFIX = "uploads/symbolcache"
# A PENDING row whose build task has not started by then was probably lost
# (e.g. a queue flush); the next event to see it queues the build again.
REQUEUE_AFTER = timedelta(seconds=60)
# A BUILDING claim older than this belongs to a worker that died.
STALE_BUILD = timedelta(minutes=10)
# A failed build is retried this often, at most MAX_ATTEMPTS times in all,
# unless it failed on the input itself (see tasks.build_cache).
RETRY_FAILED_AFTER = timedelta(hours=1)
MAX_ATTEMPTS = 3


@functools.cache
def format_tag(kind: CacheKind) -> str:
    return cache_format_tag(CacheKind(kind).label)


def storage_name(kind: CacheKind, cache_key: str) -> str:
    digest = hashlib.sha1(cache_key.encode()).hexdigest()
    return f"{STORAGE_PREFIX}/{CacheKind(kind).label}/{format_tag(kind)}/{digest}"


@dataclass(frozen=True)
class CacheSource:
    """Which artifact a cache is built from."""

    kind: CacheKind
    cache_key: str
    source_blob_id: int
    secondary_blob_id: int | None = None


# --- Observers ---------------------------------------------------------------

_observers: list = []


def add_observer(callback):
    """Call ``callback(event)`` on every cache event: "hit" (RAM tier),
    "miss", "disk_hit", "storage_load", "build" (a build ran), "unavailable"
    (the event went on without the cache)."""
    _observers.append(callback)


def remove_observer(callback):
    _observers.remove(callback)


def _notify(event: str):
    for callback in _observers:
        callback(event)


# --- RAM tier ----------------------------------------------------------------


class MemoryTier:
    """An LRU of loaded caches, bounded by their bytes.

    Holds nothing, and allocates nothing, until a cache is used. Entries unused
    for ``GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS`` are dropped by a
    timer that only runs while the tier is non-empty, so an idle process
    drains to empty. One entry may take at most a quarter of the capacity, so
    one huge bundle cannot flush everything else.
    """

    def __init__(self):
        self._entries: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @staticmethod
    def capacity() -> int:
        return settings.GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB * 1024 * 1024

    @property
    def nbytes(self) -> int:
        return self._bytes

    def __len__(self):
        return len(self._entries)

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            entry[2] = time.monotonic()
            return entry[0]

    def put(self, key, cache):
        capacity = self.capacity()
        size = cache.nbytes
        if not capacity or size > capacity // 4:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._entries[key] = [cache, size, time.monotonic()]
            self._bytes += size
            while self._bytes > capacity:
                _, (_, evicted, _) = self._entries.popitem(last=False)
                self._bytes -= evicted
            self._schedule_sweep()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_sweep(self):
        # Caller holds the lock.
        if self._timer is None and self._entries:
            idle = settings.GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS
            self._timer = threading.Timer(max(idle / 2, 1), self.sweep)
            self._timer.daemon = True
            self._timer.start()

    def sweep(self):
        idle = settings.GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS
        cutoff = time.monotonic() - idle
        with self._lock:
            self._timer = None
            for key in [k for k, e in self._entries.items() if e[2] < cutoff]:
                _, size, _ = self._entries.pop(key)
                self._bytes -= size
            self._schedule_sweep()


memory_tier = MemoryTier()


# --- Disk tier ---------------------------------------------------------------


class DiskTier:
    """Optional node-local copies of stored caches, loaded by memory map.

    Files are published whole (written to a temporary file, then renamed), so
    a reader never sees a partial file, and evicted oldest-used first once the
    directory exceeds ``GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB``. A directory
    that is missing and cannot be created, or is not writable, is logged once
    and the tier is skipped.
    """

    def __init__(self):
        # Whether each configured directory is usable, decided once.
        self._checked: dict[str, bool] = {}
        self._lock = threading.Lock()

    def reset(self):
        self._checked.clear()

    def directory(self) -> str | None:
        directory = settings.GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR
        if not directory:
            return None
        usable = self._checked.get(directory)
        if usable is None:
            usable = self._check(directory)
            self._checked[directory] = usable
        return directory if usable else None

    @staticmethod
    def _check(directory: str) -> bool:
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
        except OSError as err:
            logger.warning(
                "Symbolication disk cache directory %s is not usable (%s); "
                "caches are not kept on local disk.",
                directory,
                err,
            )
            return False
        return True

    def path(self, directory: str, kind: CacheKind, cache_key: str) -> str:
        return os.path.join(directory, storage_name(kind, cache_key))

    def load(self, kind: CacheKind, cache_key: str):
        directory = self.directory()
        if directory is None:
            return None
        path = self.path(directory, kind, cache_key)
        try:
            cache = LOADERS[kind].load_file(path)
        except FileNotFoundError:
            return None
        except (OSError, SymbolicError):
            # Unreadable or damaged: drop it; storage still has the cache.
            with contextlib.suppress(OSError):
                os.remove(path)
            return None
        with contextlib.suppress(OSError):
            os.utime(path)
        return cache

    def publish(self, kind: CacheKind, cache_key: str, data: bytes) -> str | None:
        """Keep a local copy of a stored cache; returns its path, or None when
        the tier is off or the write failed."""
        directory = self.directory()
        if directory is None:
            return None
        path = self.path(directory, kind, cache_key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                os.replace(temp_path, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
                raise
        except OSError as err:
            # Most likely full or turned read-only: stop using it (logged once).
            logger.warning(
                "Could not write symbolication disk cache %s (%s); caches are "
                "not kept on local disk.",
                path,
                err,
            )
            self._checked[directory] = False
            return None
        self.evict(directory)
        return path

    def evict(self, directory: str):
        cap = settings.GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB * 1024 * 1024
        with self._lock:
            files = []
            for root, _, names in os.walk(directory):
                for name in names:
                    path = os.path.join(root, name)
                    with contextlib.suppress(OSError):
                        stat = os.stat(path)
                        files.append((stat.st_mtime, stat.st_size, path))
            total = sum(size for _, size, _ in files)
            for _, size, path in sorted(files):
                if total <= cap:
                    break
                with contextlib.suppress(OSError):
                    os.remove(path)
                    total -= size


disk_tier = DiskTier()


def clear_process_caches():
    """Empty this process's RAM tier (for tests and cold measurements)."""
    memory_tier.clear()


# --- Lookup ------------------------------------------------------------------


def _read_stored(name: str) -> bytes:
    with default_storage.open(name, "rb") as stored:
        return stored.read()


async def _load_ready(row: SymbolicationCache, source: CacheSource):
    """Load a READY cache from the disk tier or file storage."""
    cache = disk_tier.load(source.kind, source.cache_key)
    if cache is not None:
        _notify("disk_hit")
        return cache
    # Storage backends have no async API: one thread hop, on a tier miss only.
    data = await asyncio.to_thread(_read_stored, row.storage_name)
    _notify("storage_load")
    # Verify once, as the bytes arrive from storage; the local copy is then
    # trusted when a later process maps it.
    cache = LOADERS[source.kind].load(data, verify=True)
    disk_tier.publish(source.kind, source.cache_key, data)
    return cache


async def _request_build(source: CacheSource) -> SymbolicationCache:
    """The cache row for ``source``, created PENDING (and its build queued)
    if there is none yet."""
    tag = format_tag(source.kind)
    lookup = {"kind": source.kind, "cache_key": source.cache_key, "format_tag": tag}
    row = await SymbolicationCache.objects.filter(**lookup).afirst()
    if row is not None:
        return row
    try:
        row = await SymbolicationCache.objects.acreate(
            **lookup,
            source_blob_id=source.source_blob_id,
            secondary_blob_id=source.secondary_blob_id,
        )
    except IntegrityError:
        # Another event created it first; it queued the build.
        return await SymbolicationCache.objects.aget(**lookup)
    await _enqueue(row.id)
    return row


async def _enqueue(row_id: int):
    from .tasks import build_cache

    await build_cache.aenqueue(row_id)


async def _retry_if_due(row: SymbolicationCache) -> bool:
    """Requeue a row whose build was lost or failed transiently. Returns
    whether a build was queued."""
    now = timezone.now()
    if (
        row.state == CacheState.PENDING
        and row.updated < now - REQUEUE_AFTER
        or row.state == CacheState.BUILDING
        and row.updated < now - STALE_BUILD
    ):
        due = True
    elif (
        row.state == CacheState.FAILED
        and row.attempts < MAX_ATTEMPTS
        and row.updated < now - RETRY_FAILED_AFTER
    ):
        due = (
            await SymbolicationCache.objects.filter(
                id=row.id, state=CacheState.FAILED, updated=row.updated
            ).aupdate(state=CacheState.PENDING, updated=now)
            == 1
        )
    else:
        due = False
    if due:
        await _enqueue(row.id)
    return due


async def get_cache(source: CacheSource):
    """The loaded cache for ``source``, or None if it is not available in
    time (building, unbuildable, or its source is gone)."""
    key = (source.kind, format_tag(source.kind), source.cache_key)
    cache = memory_tier.get(key)
    if cache is not None:
        _notify("hit")
        return cache
    _notify("miss")

    row = await _request_build(source)
    if row.state != CacheState.READY:
        # The build may already have run (a synchronous task backend).
        row = await SymbolicationCache.objects.filter(id=row.id).afirst() or row
    deadline = time.monotonic() + settings.GLITCHTIP_SYMBOLICATION_BUILD_WAIT_SECONDS
    delay = 0.02
    while row.state != CacheState.READY:
        if row.state == CacheState.FAILED:
            await _retry_if_due(row)
            _notify("unavailable")
            return None
        await _retry_if_due(row)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _notify("unavailable")
            return None
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.5)
        refreshed = await SymbolicationCache.objects.filter(id=row.id).afirst()
        if refreshed is None:
            _notify("unavailable")
            return None
        row = refreshed

    try:
        cache = await _load_ready(row, source)
    except (SymbolicError, OSError):
        logger.warning(
            "Stored symbolication cache %s is unreadable; rebuilding it.",
            row.storage_name,
            exc_info=True,
        )
        if await SymbolicationCache.objects.filter(
            id=row.id, state=CacheState.READY
        ).aupdate(state=CacheState.PENDING, updated=timezone.now()):
            await _enqueue(row.id)
        _notify("unavailable")
        return None
    memory_tier.put(key, cache)
    return cache

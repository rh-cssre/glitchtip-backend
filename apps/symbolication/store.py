"""Loaded symbolication caches, and the tiers they come from.

An event asks for the cache of one artifact with ``get_cache``. The lookup
goes, cheapest first:

1. the in-process tiers: an LRU of loaded caches bounded in bytes (memory
   mapped caches are not charged to it) and, for caches too big for it, a
   small short-lived budget. Both drain to empty when idle;
2. the cache stored in the default file storage, the source of truth:
   memory-mapped in place when that storage is a node-local filesystem;
3. the optional node-local disk tier (``GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR``),
   whose files are memory-mapped;
4. otherwise the stored cache is downloaded, once per process however many
   events want it at once, to a temporary file and mapped from there;
5. a build, queued on the ``symbolication`` task queue.

An event waits for builds up to ``GLITCHTIP_SYMBOLICATION_BUILD_WAIT_SECONDS``
in total and is otherwise stored unsymbolicated; events never parse an
upload themselves. A cache an event gave up on is not waited for again for
the rest of its batch, nor, for a while, by the rest of the process.

Nothing here may fail ingest: every error ends in "no cache", logged.
"""

import asyncio
import contextlib
import errno
import functools
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.utils import timezone
from gt_rust.symbolic import (
    CacheCorruptError,
    CacheStaleError,
    ProguardCache,
    SourceMapCache,
    SymbolicError,
    SymCache,
    cache_format_tag,
)

from .models import CacheKind, CacheState, SymbolicationCache
from .sources import COPY_CHUNK, is_node_local, load_lane, local_path

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
# A running build refreshes its row's ``updated`` this often, so a long but
# healthy build is never mistaken for a dead one.
BUILD_HEARTBEAT = timedelta(seconds=60)
# A BUILDING claim not refreshed for three heartbeats belongs to a worker
# that died (a pod killed mid-build, say); the next build task or event
# takes it over.
STALE_BUILD = 3 * BUILD_HEARTBEAT
# A build that failed transiently is retried this often, at most
# MAX_ATTEMPTS times in all; one that failed on its input is not.
RETRY_FAILED_AFTER = timedelta(hours=1)
MAX_ATTEMPTS = 3
# Loaded caches kept at most, memory-mapped ones included.
MAX_LOADED_ENTRIES = 512


@functools.cache
def format_tag(kind: CacheKind) -> str:
    return cache_format_tag(CacheKind(kind).label)


def cache_path(kind: CacheKind, cache_key: str) -> str:
    """Where a cache for ``cache_key`` lives, relative to a storage root."""
    digest = hashlib.sha1(cache_key.encode()).hexdigest()
    return f"{STORAGE_PREFIX}/{CacheKind(kind).label}/{format_tag(kind)}/{digest}"


def storage_name(kind: CacheKind, cache_key: str, nonce: str) -> str:
    """The storage name of one build of a cache. Each build gets a fresh
    name, so a rebuild never overwrites bytes another process may still be
    reading or have mapped."""
    return f"{cache_path(kind, cache_key)}-{nonce}"


@dataclass(frozen=True)
class CacheSource:
    """Which artifact a cache is built from."""

    kind: CacheKind
    # Starts with the source blob's SHA-1 checksum.
    cache_key: str
    source_blob_id: int
    secondary_blob_id: int | None = None

    @property
    def source_digest(self) -> bytes | None:
        """The source blob's raw SHA-1, as recorded in the cache header."""
        return source_digest(self.cache_key.split(":", 1)[0])


def source_digest(checksum: str) -> bytes | None:
    try:
        return bytes.fromhex(checksum)
    except ValueError:
        return None


@dataclass
class LookupContext:
    """One event's lookups: its remaining wait budget, the organization it
    belongs to, and the keys its batch has already given up on."""

    deadline: float
    organization_id: int | None = None
    unavailable: set = field(default_factory=set)

    @classmethod
    def for_event(cls, organization_id=None, batch_unavailable=None):
        return cls(
            deadline=time.monotonic()
            + settings.GLITCHTIP_SYMBOLICATION_BUILD_WAIT_SECONDS,
            organization_id=organization_id,
            unavailable=batch_unavailable if batch_unavailable is not None else set(),
        )

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


# --- Observers ---------------------------------------------------------------

_observers: list = []


def add_observer(callback):
    """Call ``callback(event)`` on every cache event: "hit" (in-process),
    "miss", "storage_map" (a local stored cache mapped in place),
    "disk_hit", "storage_load" (downloaded), "shared_load" (waited on
    another event's load), "build" (a build ran), "unavailable" (the event
    went on without the cache)."""
    _observers.append(callback)


def remove_observer(callback):
    _observers.remove(callback)


def _notify(event: str):
    for callback in _observers:
        callback(event)


# --- In-process tiers --------------------------------------------------------


class LoadedCaches:
    """An LRU of loaded caches, bounded in bytes, that drains when idle.

    Holds nothing, and allocates nothing, until a cache is used. Entries unused
    for ``idle()`` seconds are dropped by a timer that only runs while the
    tier is non-empty, so an idle process drains to empty. A memory-mapped
    cache is charged nothing: its pages belong to the page cache, which the
    kernel reclaims. The number of entries is bounded all the same.
    """

    def __init__(self, capacity, idle, entry_limit, admit_one_oversized=False):
        self._capacity = capacity
        self._idle = idle
        self._entry_limit = entry_limit
        self._admit_one_oversized = admit_one_oversized
        self._entries: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

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

    def put(self, key, cache, mapped: bool) -> bool:
        """Keep ``cache``; False if this tier will not take it."""
        capacity = self._capacity()
        if not capacity or self._idle() <= 0:
            return False
        charge = 0 if mapped else cache.nbytes
        if charge > self._entry_limit(capacity) and not (
            self._admit_one_oversized and charge > capacity
        ):
            return False
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            if charge > capacity:
                # Alone it may exceed the budget; nothing else stays.
                self._entries.clear()
                self._bytes = 0
            self._entries[key] = [cache, charge, time.monotonic()]
            self._bytes += charge
            while len(self._entries) > 1 and (
                self._bytes > capacity or len(self._entries) > MAX_LOADED_ENTRIES
            ):
                _, (_, evicted, _) = self._entries.popitem(last=False)
                self._bytes -= evicted
            self._schedule_sweep()
        return True

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
            self._timer = threading.Timer(max(self._idle() / 2, 0.1), self.sweep)
            self._timer.daemon = True
            self._timer.start()

    def sweep(self):
        cutoff = time.monotonic() - self._idle()
        with self._lock:
            self._timer = None
            for key in [k for k, e in self._entries.items() if e[2] <= cutoff]:
                _, size, _ = self._entries.pop(key)
                self._bytes -= size
            self._schedule_sweep()


def _mb(name):
    return lambda: getattr(settings, name) * 1024 * 1024


def _seconds(name):
    return lambda: getattr(settings, name)


memory_tier = LoadedCaches(
    capacity=_mb("GLITCHTIP_SYMBOLICATION_RAM_CACHE_MB"),
    idle=_seconds("GLITCHTIP_SYMBOLICATION_RAM_CACHE_IDLE_SECONDS"),
    # One cache may take a quarter, so one huge bundle cannot flush the rest.
    entry_limit=lambda capacity: capacity // 4,
)
# Caches too big for the RAM tier: a separate small budget with a short TTL,
# so a run of events on one large artifact (or two in one event) shares a
# copy instead of loading it from storage every time.
oversized_tier = LoadedCaches(
    capacity=_mb("GLITCHTIP_SYMBOLICATION_OVERSIZED_MB"),
    idle=_seconds("GLITCHTIP_SYMBOLICATION_OVERSIZED_TTL_SECONDS"),
    entry_limit=lambda capacity: capacity,
    admit_one_oversized=True,
)


class NegativeCache:
    """Keys an event recently gave up on, so the process does not wait for
    them again for GLITCHTIP_SYMBOLICATION_UNAVAILABLE_SECONDS. A key that
    was still building is re-checked without waiting; a failed one is
    skipped outright."""

    def __init__(self):
        self._until: dict = {}
        self._lock = threading.Lock()

    def get(self, key) -> str | None:
        with self._lock:
            entry = self._until.get(key)
            if entry is None:
                return None
            until, reason = entry
            if until <= time.monotonic():
                del self._until[key]
                return None
            return reason

    def put(self, key, reason: str):
        seconds = settings.GLITCHTIP_SYMBOLICATION_UNAVAILABLE_SECONDS
        if seconds <= 0:
            return
        with self._lock:
            if len(self._until) > 10_000:
                now = time.monotonic()
                self._until = {k: v for k, v in self._until.items() if v[0] > now}
            self._until[key] = (time.monotonic() + seconds, reason)

    def clear(self):
        with self._lock:
            self._until.clear()


negative_cache = NegativeCache()


# --- Disk tier ---------------------------------------------------------------


class DiskTier:
    """Optional node-local copies of stored caches, loaded by memory map.

    Files are published whole (written to a temporary file, then renamed), so
    a reader never sees a partial file, and evicted oldest-used first once
    the tier's subtree exceeds ``GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB``.
    The directory must belong to this process's user and not be writable by
    group or others, since its files are then trusted. A directory that is
    missing and cannot be created, or is not safe, is logged once and the
    tier is skipped; a failed write only pauses the tier for a minute.
    """

    PAUSE_SECONDS = 60
    TEMP_PREFIX = ".tmp-"

    def __init__(self):
        # Whether each configured directory is usable, decided once.
        self._checked: dict[str, bool] = {}
        self._paused_until: dict[str, float] = {}
        # Files this process has verified since it mapped them.
        self._verified: set[str] = set()
        self._lock = threading.Lock()

    def reset(self):
        self._checked.clear()
        self._paused_until.clear()
        self._verified.clear()

    def directory(self) -> str | None:
        directory = settings.GLITCHTIP_SYMBOLICATION_DISK_CACHE_DIR
        if not directory:
            return None
        usable = self._checked.get(directory)
        if usable is None:
            usable = self._check(directory)
            self._checked[directory] = usable
        if not usable:
            return None
        if self._paused_until.get(directory, 0) > time.monotonic():
            return None
        return directory

    @staticmethod
    def _check(directory: str) -> bool:
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            info = os.stat(directory)
            if info.st_uid != os.getuid():
                raise PermissionError("not owned by this process's user")
            if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise PermissionError("writable by group or others")
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

    def _pause(self, directory: str, err: OSError):
        logger.warning(
            "Could not write symbolication disk cache in %s (%s); retrying "
            "in %d seconds.",
            directory,
            err,
            self.PAUSE_SECONDS,
        )
        self._paused_until[directory] = time.monotonic() + self.PAUSE_SECONDS

    def load(self, kind: CacheKind, cache_key: str, digest: bytes | None):
        """Map a local copy, or None. Blocking: run it on the load lane.

        The file is read ahead into the page cache first. The mapping is
        read at random, and a lookup that faults pages in from disk on the
        event loop costs around a millisecond a frame; faulting them in here,
        off the loop, costs the loop nothing.
        """
        directory = self.directory()
        if directory is None:
            return None
        path = os.path.join(directory, cache_path(kind, cache_key))
        verify = path not in self._verified
        try:
            _read_ahead(path)
            cache = LOADERS[kind].load_file(
                path, verify=verify, expected_source_digest=digest
            )
        except FileNotFoundError:
            return None
        except (OSError, SymbolicError):
            # Unreadable or damaged: drop it; storage still has the cache.
            with contextlib.suppress(OSError):
                os.remove(path)
            return None
        self._verified.add(path)
        with contextlib.suppress(OSError):
            os.utime(path)
        return cache

    def publish(self, kind: CacheKind, cache_key: str, source) -> str | None:
        """Copy a stored cache (an open file) into the tier; returns the local
        path, or None when the tier is off or the write failed."""
        directory = self.directory()
        if directory is None:
            return None
        path = os.path.join(directory, cache_path(kind, cache_key))
        for attempt in range(2):
            try:
                self._write(path, source)
                break
            except OSError as err:
                if attempt == 0 and err.errno == errno.ENOSPC:
                    self.evict(directory)
                    source.seek(0)
                    continue
                self._pause(directory, err)
                return None
        self._verified.add(path)  # written by this process from verified bytes
        self.evict(directory)
        return path

    def _write(self, path: str, source):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=self.TEMP_PREFIX
        )
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(source, out, COPY_CHUNK)
            os.replace(temp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(temp_path)
            raise

    def evict(self, directory: str):
        """Keep the tier's own subtree under its cap, oldest-used first."""
        cap = settings.GLITCHTIP_SYMBOLICATION_DISK_CACHE_MB * 1024 * 1024
        root = os.path.join(directory, STORAGE_PREFIX)
        young = time.time() - 60
        with self._lock:
            files = []
            for folder, _, names in os.walk(root):
                for name in names:
                    path = os.path.join(folder, name)
                    with contextlib.suppress(OSError):
                        info = os.stat(path)
                        if name.startswith(self.TEMP_PREFIX) and info.st_mtime > young:
                            continue  # another process is writing it
                        files.append((info.st_mtime, info.st_size, path))
            total = sum(size for _, size, _ in files)
            for _, size, path in sorted(files):
                if total <= cap:
                    break
                with contextlib.suppress(OSError):
                    os.remove(path)
                    self._verified.discard(path)
                    total -= size


_READ_AHEAD_BUFFER = 64 * 1024


def _read_ahead(path: str):
    with open(path, "rb", buffering=0) as local:
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(local.fileno(), 0, 0, os.POSIX_FADV_WILLNEED)
        buffer = bytearray(_READ_AHEAD_BUFFER)
        while local.readinto(buffer):
            pass


disk_tier = DiskTier()


def clear_process_caches():
    """Empty this process's in-memory caches (for tests and cold
    measurements)."""
    memory_tier.clear()
    oversized_tier.clear()
    negative_cache.clear()


# --- Loading a stored cache --------------------------------------------------


def _mappable_stored_path(name: str) -> str | None:
    """The stored cache's path, if it is safe to memory-map in place: local
    file storage on a filesystem only this node writes to. A map of a file
    another NFS client deletes faults this process."""
    path = local_path(default_storage, name)
    if path is not None and is_node_local(path):
        return path
    return None


def _load_from_tiers(name: str, source: CacheSource):
    """Load a READY cache, cheapest source first.

    Blocking (storage has no async API, and verifying a large cache takes
    milliseconds), so it runs on the load lane. Returns the cache, where it
    came from, and whether it is memory-mapped.
    """
    loader = LOADERS[source.kind]
    digest = source.source_digest
    # Local file storage: memory-map the stored file itself. That relies on a
    # stored cache never being rewritten in place (see tasks._build).
    path = _mappable_stored_path(name)
    if path is not None:
        cache = loader.load_file(path, verify=True, expected_source_digest=digest)
        return cache, "storage_map", True
    cache = disk_tier.load(source.kind, source.cache_key, digest)
    if cache is not None:
        return cache, "disk_hit", True
    with default_storage.open(name, "rb") as stored:
        local = disk_tier.publish(source.kind, source.cache_key, stored)
        if local is not None:
            try:
                cache = loader.load_file(
                    local, verify=True, expected_source_digest=digest
                )
            except SymbolicError:
                with contextlib.suppress(OSError):
                    os.remove(local)
                raise
            return cache, "storage_load", True
        stored.seek(0)
        return _load_via_temp_file(loader, stored, digest)


def _load_via_temp_file(loader, stored, digest):
    """Copy a downloaded cache to a temporary file and map it, so it never
    sits on the Python heap. Unlinking the file keeps the map valid. Without
    a writable temporary directory, read it into bytes instead."""
    try:
        temp = tempfile.NamedTemporaryFile(prefix="gt-symbol-cache-")
    except OSError:
        data = stored.read()
        return (
            loader.load(data, verify=True, expected_source_digest=digest),
            "storage_load",
            False,
        )
    with temp:
        shutil.copyfileobj(stored, temp, COPY_CHUNK)
        temp.flush()
        cache = loader.load_file(temp.name, verify=True, expected_source_digest=digest)
    return cache, "storage_load", True


# Loads in flight, by cache key: concurrent events for one artifact (on any
# of the process's event loops) wait on one load instead of each fetching
# the cache from storage.
_loads_in_flight: dict = {}
_loads_lock = threading.Lock()


async def _load_ready(row: SymbolicationCache, source: CacheSource, key):
    """Load a READY cache off the loop; see _load_from_tiers."""
    with _loads_lock:
        future = _loads_in_flight.get(key)
        leader = future is None
        if leader:
            future = load_lane.submit(_load_from_tiers, row.storage_name, source)
            _loads_in_flight[key] = future
    if leader:
        # Outside the lock: a load that already finished runs the callback
        # right here, and the callback takes the lock.
        future.add_done_callback(lambda _: _forget_load(key, future))
    # Shielded: one event being cancelled must not cancel the load the
    # others wait on.
    cache, tier, mapped = await asyncio.wait_for(
        asyncio.shield(asyncio.wrap_future(future)),
        settings.GLITCHTIP_SYMBOLICATION_LOAD_TIMEOUT_SECONDS,
    )
    _notify(tier if leader else "shared_load")
    return cache, mapped


def _forget_load(key, future):
    with _loads_lock:
        if _loads_in_flight.get(key) is future:
            del _loads_in_flight[key]


def _delete_stored(name: str):
    with contextlib.suppress(Exception):
        default_storage.delete(name)


# --- Rows and builds ---------------------------------------------------------


async def _enqueue(row_id: int, organization_id: int | None):
    from .tasks import build_cache

    await build_cache.aenqueue(row_id, organization_id)


def _source_missing(row: SymbolicationCache) -> bool:
    return row.source_blob_id is None or (
        row.kind == CacheKind.SOURCEMAP and row.secondary_blob_id is None
    )


async def _request_build(source: CacheSource, organization_id: int | None):
    """The cache row for ``source``, created PENDING (and its build queued)
    if there is none yet. None when the organization already has its fill of
    builds outstanding."""
    tag = format_tag(source.kind)
    lookup = {"kind": source.kind, "cache_key": source.cache_key, "format_tag": tag}
    row = await SymbolicationCache.objects.filter(**lookup).afirst()
    if row is not None:
        if row.state != CacheState.READY and _source_missing(row):
            # Its source was deleted and uploaded again: point the row at the
            # new upload and build it afresh.
            repointed = await SymbolicationCache.objects.filter(
                id=row.id, state=row.state, updated=row.updated
            ).aupdate(
                source_blob_id=source.source_blob_id,
                secondary_blob_id=source.secondary_blob_id,
                state=CacheState.PENDING,
                attempts=0,
                error="",
                updated=timezone.now(),
            )
            if repointed:
                await _enqueue(row.id, organization_id)
                row = await SymbolicationCache.objects.aget(id=row.id)
        return row

    cap = settings.GLITCHTIP_SYMBOLICATION_MAX_PENDING_PER_ORG
    if organization_id is not None and cap > 0:
        outstanding = await SymbolicationCache.objects.filter(
            requested_by_organization_id=organization_id,
            state__in=[CacheState.PENDING, CacheState.BUILDING],
        ).acount()
        if outstanding >= cap:
            return None
    try:
        row = await SymbolicationCache.objects.acreate(
            **lookup,
            source_blob_id=source.source_blob_id,
            secondary_blob_id=source.secondary_blob_id,
            requested_by_organization_id=organization_id,
        )
    except IntegrityError:
        # Another event created it first; it queued the build.
        return await SymbolicationCache.objects.aget(**lookup)
    await _enqueue(row.id, organization_id)
    return row


async def _retry_if_due(row: SymbolicationCache, organization_id) -> bool:
    """Requeue a row whose build was lost, died, or failed transiently.

    Each case is a conditional UPDATE that moves the row on, so of any number
    of events seeing the same row at once, one requeues it. Returns whether
    a build was queued.
    """
    now = timezone.now()
    same = SymbolicationCache.objects.filter(
        id=row.id, state=row.state, updated=row.updated
    )
    if row.state == CacheState.PENDING and row.updated < now - REQUEUE_AFTER:
        moved = await same.aupdate(updated=now)
    elif row.state == CacheState.BUILDING and row.updated < now - STALE_BUILD:
        if row.attempts >= MAX_ATTEMPTS:
            if await same.aupdate(
                state=CacheState.FAILED, error="build did not finish", updated=now
            ):
                logger.warning(
                    "Symbolication cache %s build never finished; giving up.",
                    row.id,
                )
            return False
        moved = await same.aupdate(state=CacheState.PENDING, updated=now)
    elif (
        row.state == CacheState.FAILED
        and row.attempts < MAX_ATTEMPTS
        and row.updated < now - RETRY_FAILED_AFTER
    ):
        moved = await same.aupdate(state=CacheState.PENDING, updated=now)
    else:
        return False
    if moved:
        await _enqueue(row.id, organization_id)
    return bool(moved)


async def _wait_until_settled(row, context: LookupContext, wait: bool):
    """Poll ``row`` until it is READY or FAILED, within the event's budget.
    Returns the last row seen (None if it vanished)."""
    delay = 0.02
    while (
        wait
        and row is not None
        and row.state
        not in (
            CacheState.READY,
            CacheState.FAILED,
        )
    ):
        remaining = context.remaining()
        if remaining <= 0:
            break
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.5)
        row = await SymbolicationCache.objects.filter(id=row.id).afirst()
    return row


async def _get_uncached(source: CacheSource, key, context: LookupContext, wait: bool):
    row = await _request_build(source, context.organization_id)
    if row is None:
        return None, "pending"
    if row.state != CacheState.READY:
        # The build may already have run (a synchronous task backend).
        row = await SymbolicationCache.objects.filter(id=row.id).afirst()
    if row is None:
        return None, "pending"
    if row.state != CacheState.READY:
        if await _retry_if_due(row, context.organization_id):
            row = await SymbolicationCache.objects.filter(id=row.id).afirst()
        if row is not None and row.state != CacheState.FAILED:
            row = await _wait_until_settled(row, context, wait)
    if row is None or row.state != CacheState.READY:
        failed = row is not None and row.state == CacheState.FAILED
        return None, "failed" if failed else "pending"

    try:
        cache, mapped = await _load_ready(row, source, key)
    except CacheStaleError:
        # Written by another format generation: not damage, just rebuild.
        logger.info("Rebuilding stale symbolication cache %s", row.id)
        await _rebuild(row, context.organization_id, delete_stored=False)
        return None, "pending"
    except (SymbolicError, OSError) as err:
        # CacheCorruptError, an unreadable file, or a cache that does not
        # match its source: discard it and rebuild from the source.
        logger.warning(
            "Stored symbolication cache %s is unusable (%s); rebuilding it.",
            row.id,
            type(err).__name__,
        )
        await _rebuild(
            row,
            context.organization_id,
            delete_stored=isinstance(err, CacheCorruptError),
        )
        return None, "pending"
    if not memory_tier.put(key, cache, mapped):
        oversized_tier.put(key, cache, mapped)
    return cache, None


async def _rebuild(row: SymbolicationCache, organization_id, delete_stored: bool):
    """Send a READY row back to be built again. Its stored bytes are deleted
    only if the row still points at them: a rebuild another event already
    finished must keep its cache."""
    moved = await SymbolicationCache.objects.filter(
        id=row.id, state=CacheState.READY, storage_name=row.storage_name
    ).aupdate(
        state=CacheState.PENDING,
        storage_name="" if delete_stored else row.storage_name,
        updated=timezone.now(),
    )
    if moved:
        if delete_stored:
            await load_lane.run(_delete_stored, row.storage_name)
        await _enqueue(row.id, organization_id)


async def get_cache(source: CacheSource, context: LookupContext | None = None):
    """The loaded cache for ``source``, or None if it is not available in
    time (building, unbuildable, its source is gone, or anything failed)."""
    context = context or LookupContext.for_event()
    key = (source.kind, format_tag(source.kind), source.cache_key)
    cache = memory_tier.get(key) or oversized_tier.get(key)
    if cache is not None:
        _notify("hit")
        return cache
    if key in context.unavailable:
        _notify("unavailable")
        return None
    _notify("miss")
    known = negative_cache.get(key)
    if known == "failed":
        context.unavailable.add(key)
        _notify("unavailable")
        return None
    try:
        cache, reason = await _get_uncached(source, key, context, wait=known is None)
    except Exception as err:
        # Symbolication never fails ingest: a storage, queue or database
        # error (a missing table included) only costs this event its cache.
        logger.warning(
            "Symbolication cache lookup failed (%s); going on without it.",
            type(err).__name__,
        )
        cache, reason = None, "pending"
    if cache is None:
        context.unavailable.add(key)
        negative_cache.put(key, reason)
        _notify("unavailable")
    return cache

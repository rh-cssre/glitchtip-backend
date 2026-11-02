"""Access to uploaded source artifacts (FileBlobs) without whole-file reads,
and the bounded thread pools symbolication's blocking I/O runs on."""

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor

from django.conf import settings

from sentry.utils.zip import ZipExtractionLimitExceeded, _reject_oversized_input

logger = logging.getLogger(__name__)

COPY_CHUNK = 1024 * 1024
# Source context reads: the largest member read from a source bundle, and
# the most paths read for one event.
MAX_SOURCE_MEMBER_BYTES = 10 * 1024 * 1024
MAX_SOURCE_PATHS = 64


# --- Bounded pools -----------------------------------------------------------


class _Lane:
    """A small, fixed pool created on first use.

    Storage backends have no async API, so symbolication's blocking reads
    run on these instead of the loop's default executor: a burst of misses
    cannot fan out over many threads (each new thread grows its own malloc
    arena), and each kind of work has its own lane, so slow source-bundle
    reads cannot hold up cache loads.
    """

    def __init__(self, name: str, threads: int):
        self._name = name
        self._threads = threads
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def submit(self, func, *args) -> Future:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._threads, thread_name_prefix=self._name
                )
        return self._executor.submit(func, *args)

    async def run(self, func, *args, timeout: float | None = None):
        future = asyncio.wrap_future(self.submit(func, *args))
        if timeout is None:
            return await future
        return await asyncio.wait_for(future, timeout)


# Longest wait for one stored cache load or source bundle read.
LOAD_TIMEOUT_SECONDS = 10.0

# Loading stored caches, and CPU work too slow for the event loop.
load_lane = _Lane("symbolication-read", 2)
# Reading source-bundle members for source context.
bundle_lane = _Lane("symbolication-bundle", 1)


def submit_storage_read(func, *args) -> Future:
    return load_lane.submit(func, *args)


async def run_storage_read(func, *args):
    return await load_lane.run(func, *args)


# --- Build inputs -------------------------------------------------------------

_warned_no_temp_dir = False


def _warn_no_temp_dir(err: OSError):
    global _warned_no_temp_dir
    if not _warned_no_temp_dir:
        _warned_no_temp_dir = True
        logger.warning(
            "No writable temporary directory (%s); symbolication builds read "
            "their inputs into memory instead.",
            err,
        )


@contextlib.contextmanager
def blob_input(file_blob):
    """The blob's bytes as a build input: a local path when possible.

    With local file storage that is the stored file itself, which the Rust
    side memory-maps. Otherwise the blob is streamed to a temporary file in
    chunks, and the storage file is closed before the path is used, so the
    blob is never held in memory whole. Only if no temporary file can be
    created (a read-only root with no writable /tmp) is the blob read into
    bytes: that costs its size in memory, but builds run one at a time on
    their own queue. gt_rust takes either form.
    """
    field_file = file_blob.blob
    path = local_path(field_file.storage, field_file.name)
    if path is not None:
        yield path
        return
    try:
        target = tempfile.NamedTemporaryFile(prefix="gt-symbol-source-")
    except OSError as err:
        _warn_no_temp_dir(err)
        with field_file.storage.open(field_file.name, "rb") as source:
            data = source.read()
        yield data
        return
    with target:
        with field_file.storage.open(field_file.name, "rb") as source:
            shutil.copyfileobj(source, target, COPY_CHUNK)
        target.flush()
        yield target.name


def local_path(storage, name: str) -> str | None:
    """The file's path when ``storage`` keeps it on a local filesystem."""
    try:
        path = storage.path(name)
    except NotImplementedError:
        return None
    # Some backends report a path without keeping the file there (the
    # in-memory storage), so check it exists.
    return path if os.path.isfile(path) else None


# --- Node-local filesystems --------------------------------------------------

# Filesystems a memory map must not be taken of: another client can delete
# or rewrite the file under the map, which then faults (SIGBUS) this process.
NETWORK_FILESYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb",
        "smb3",
        "smbfs",
        "ceph",
        "cephfs",
        "glusterfs",
        "9p",
        "afs",
        "gfs2",
        "ocfs2",
        "lustre",
        "beegfs",
        "gpfs",
        "davfs",
        "sshfs",
        "fuse",
    }
)
MOUNTINFO = "/proc/self/mountinfo"


@functools.lru_cache(maxsize=64)
def _filesystem_type(device: int) -> str | None:
    """The mounted filesystem type of a device number, from mountinfo."""
    wanted = f"{os.major(device)}:{os.minor(device)}"
    try:
        with open(MOUNTINFO) as mountinfo:
            for line in mountinfo:
                fields = line.split()
                if len(fields) > 2 and fields[2] == wanted and "-" in fields:
                    return fields[fields.index("-") + 1]
    except OSError:
        return None
    return None


def is_node_local(path: str) -> bool:
    """Whether ``path`` is on a filesystem only this node writes to.

    Linux only (it reads /proc/self/mountinfo); anywhere else, or when the
    filesystem cannot be determined, the answer is no.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        device = os.stat(path).st_dev
    except OSError:
        return False
    fstype = _filesystem_type(device)
    if fstype is None:
        return False
    return fstype not in NETWORK_FILESYSTEMS and not fstype.startswith("fuse.")


# --- Source bundles ----------------------------------------------------------


def read_bundle_members(file_blob, member_names) -> dict[str, list[str]]:
    """Read text members of a zip-format source bundle, as lists of lines.

    The zip is read in place (its directory, then only the named members), not
    copied out whole. Its size and declared directory are checked before the
    zip is opened, as for uploaded archives; a member over
    MAX_SOURCE_MEMBER_BYTES is skipped unread. Members that are missing are
    left out of the result.
    """
    field_file = file_blob.blob
    lines = {}
    with field_file.storage.open(field_file.name, "rb") as stored:
        try:
            _reject_oversized_input(
                stored,
                settings.GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE,
                settings.GLITCHTIP_MAX_ARCHIVE_MEMBERS,
            )
        except ZipExtractionLimitExceeded:
            logger.info("Source bundle %s is over the archive limits", file_blob.id)
            return lines
        with zipfile.ZipFile(stored) as zf:
            for member in list(member_names)[:MAX_SOURCE_PATHS]:
                try:
                    info = zf.getinfo(member)
                except KeyError:
                    continue
                if info.file_size > MAX_SOURCE_MEMBER_BYTES:
                    continue
                lines[member] = zf.read(info).decode("utf-8").splitlines()
    return lines

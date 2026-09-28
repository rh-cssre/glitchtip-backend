"""Access to uploaded source artifacts (FileBlobs) without whole-file reads."""

import asyncio
import contextlib
import os
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

COPY_CHUNK = 1024 * 1024
# Threads for storage reads on the ingest path (a stored cache on a tier
# miss, source bundle members). Storage backends have no async API; a small
# fixed pool keeps a burst of misses from fanning out over the loop's default
# executor, where each new thread would grow its own malloc arena.
STORAGE_READ_THREADS = 2

_read_executor: ThreadPoolExecutor | None = None
_read_executor_lock = threading.Lock()


async def run_storage_read(func, *args):
    """Run a blocking storage read on the bounded read pool."""
    global _read_executor
    with _read_executor_lock:
        if _read_executor is None:
            _read_executor = ThreadPoolExecutor(
                max_workers=STORAGE_READ_THREADS,
                thread_name_prefix="symbolication-read",
            )
    return await asyncio.get_running_loop().run_in_executor(_read_executor, func, *args)


@contextlib.contextmanager
def blob_path(file_blob):
    """A local filesystem path holding the blob's bytes.

    With local file storage that is the stored file itself, which the Rust
    side memory-maps. Otherwise the blob is streamed to a temporary file in
    chunks, so it is never held in memory whole.
    """
    field_file = file_blob.blob
    try:
        path = field_file.storage.path(field_file.name)
    except NotImplementedError:
        path = None
    # Some backends report a path without keeping the file there (the
    # in-memory storage), so check it exists.
    if path is not None and os.path.isfile(path):
        yield path
        return
    with (
        field_file.storage.open(field_file.name, "rb") as source,
        tempfile.NamedTemporaryFile(prefix="gt-symbol-source-") as target,
    ):
        shutil.copyfileobj(source, target, COPY_CHUNK)
        target.flush()
        yield target.name


def read_bundle_members(file_blob, member_names) -> dict[str, list[str]]:
    """Read text members of a zip-format source bundle, as lists of lines.

    The zip is read in place (its directory, then only the named members), not
    copied out whole. Members that are missing are left out of the result.
    """
    field_file = file_blob.blob
    lines = {}
    with (
        field_file.storage.open(field_file.name, "rb") as stored,
        zipfile.ZipFile(stored) as zf,
    ):
        names = set(zf.namelist())
        for member in member_names:
            if member in names:
                lines[member] = zf.read(member).decode("utf-8").splitlines()
    return lines

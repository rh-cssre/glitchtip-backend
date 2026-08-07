import os
import zipfile

from django.conf import settings

# Bytes pulled out of a member per read. Small enough that the running total
# is checked often, large enough to keep the syscall count reasonable.
EXTRACT_CHUNK_SIZE = 64 * 1024


class ZipExtractionLimitExceeded(Exception):
    """A zip archive expanded past a configured extraction limit.

    Callers should treat this as a rejected upload, not a server error: it
    means the archive is larger (in bytes or in entries) than extraction is
    willing to materialize, not that extraction failed.
    """


def is_unsafe_path(path):
    if os.path.isabs(path):
        return True
    for segment in path.split(os.path.sep):
        if segment == os.path.pardir:
            return True
    return False


def find_common_prefix(members):
    qualifying_members = []
    for member in members:
        pieces = member.split("/")
        if pieces and pieces[0].startswith("."):
            continue
        qualifying_members.append(pieces)

    rv = os.path.commonprefix(qualifying_members)
    if rv:
        return rv[0] + "/"
    return ""


def _makedirs_tracked(directory, created_dirs):
    """``os.makedirs`` that records exactly which levels this call created.

    Only levels created here are recorded, so cleanup after a rejected
    archive never removes a directory that was already there.
    """
    if not directory:
        return
    missing = []
    current = directory
    while current and not os.path.isdir(current):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return
    created_dirs.extend(missing)


def _remove_partial_output(created_files, created_dirs):
    """Undo the writes of an aborted extraction.

    An archive rejected halfway through has already put bytes on disk; leaving
    them behind would defeat the size limit it was rejected by.
    """
    for file_path in created_files:
        try:
            os.unlink(file_path)
        except OSError:
            pass
    # Deepest first: a parent is only removable once its children are gone.
    for directory in sorted(created_dirs, key=len, reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def _extract_member(zf, member, dst_path, remaining):
    """Copy one member to ``dst_path``, returning the bytes written.

    The budget is spent against bytes read out of the archive rather than the
    declared ``file_size`` of its members. Declared sizes live in the archive
    directory, which whoever built the archive controls, and they do not add
    up to what extraction writes: a directory holding several entries under
    one name makes extraction write the member that name resolves to once per
    entry, while the declared sizes account for it once. Counting where the
    writing happens is the form that cannot be talked out of the limit.
    """
    written = 0
    with open(dst_path, "wb") as df, zf.open(member) as sf:
        while chunk := sf.read(EXTRACT_CHUNK_SIZE):
            written += len(chunk)
            if written > remaining:
                raise ZipExtractionLimitExceeded(
                    "archive expands past the extraction size limit"
                )
            df.write(chunk)
    return written


def safe_extract_zip(
    f, path, strip_toplevel=True, max_total_bytes=None, max_members=None
):
    """Safely extract a given zip file to a path.  The zipfile can either
    be an open file or a filename.  If the zip is unsafe an exception is
    raised.  Optionally the toplevel folder is stripped off.  If there are
    hidden files on toplevel then, these are silently ignored.

    Extraction is bounded in two ways, because a zip's compression ratio has
    no ceiling and the caller has no way to know how far a given archive
    expands before it tries: ``max_total_bytes`` caps the bytes written across
    all members and ``max_members`` caps the entry count. Both default to the
    corresponding Django settings. Breaching either raises
    ``ZipExtractionLimitExceeded`` and removes everything this call wrote, so
    a rejected archive leaves nothing behind.
    """
    if max_total_bytes is None:
        max_total_bytes = settings.GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE
    if max_members is None:
        max_members = settings.GLITCHTIP_MAX_ARCHIVE_MEMBERS

    close = False
    if not isinstance(f, zipfile.ZipFile):
        close = isinstance(f, str)
        zf = zipfile.ZipFile(f, "r")
    else:
        zf = f
    created_files: list[str] = []
    created_dirs: list[str] = []
    try:
        members = zf.namelist()
        # Entries in the archive directory, which is an upper bound on the
        # files extraction can create.
        if len(members) > max_members:
            raise ZipExtractionLimitExceeded(
                f"archive has {len(members)} entries, over the "
                f"{max_members} entry limit"
            )
        # Cheap early reject for an archive that admits up front it is too
        # big, so an obvious bomb costs no disk writes at all. Advisory only:
        # these are declared sizes, and the per-member count below is what
        # actually holds the line.
        if sum(info.file_size for info in zf.infolist()) > max_total_bytes:
            raise ZipExtractionLimitExceeded(
                "archive declares more than the extraction size limit"
            )

        if strip_toplevel:
            prefix = find_common_prefix(members)
        else:
            prefix = ""
        remaining = max_total_bytes
        for member in members:
            # Skip directories
            if member.endswith("/"):
                continue

            if not member.startswith(prefix) or is_unsafe_path(member):
                continue
            dst_path = os.path.join(path, member[len(prefix) :])
            _makedirs_tracked(os.path.dirname(dst_path), created_dirs)
            created_files.append(dst_path)
            remaining -= _extract_member(zf, member, dst_path, remaining)
    except BaseException:
        _remove_partial_output(created_files, created_dirs)
        raise
    finally:
        if close:
            zf.close()

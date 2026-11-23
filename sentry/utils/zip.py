import os
import struct
import zipfile

from django.conf import settings

# Bytes pulled out of a member per read. Small enough that the running total
# is checked often, large enough to keep the syscall count reasonable. Same
# read-accumulate-reject-past-the-cap shape as apps/files/api.py's
# decompress_chunk (CHUNK_READ_SIZE) and the ingest seam in
# apps/event_ingest/rust_envelope.py; sentry/ importing from apps/ would be
# backwards, so the constant is repeated rather than shared.
EXTRACT_CHUNK_SIZE = 64 * 1024

# Archive directory bytes allowed per member the caller is willing to accept.
# zipfile builds one ZipInfo per directory header, plus a dict keyed by name,
# while the ZipFile is being constructed -- before any limit below can look at
# the entry count. Bounding the directory is therefore the only way to bound
# that table. A header is 46 bytes plus its name, so 512 leaves room for a path
# far longer than a real bundle uses and never refuses a legitimate archive,
# while a directory packed with minimum-size headers still resolves to a
# bounded multiple of max_members rather than to whatever the uploader sent.
_DIRECTORY_BYTES_PER_MEMBER = 512

# End-of-central-directory record: a signature, then fixed fields, one of
# which (offset 12) is the size of the central directory. It is the last thing
# in an archive apart from an optional comment.
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_SIZE = 22
_EOCD_DIRECTORY_SIZE_OFFSET = 12
_MAX_ARCHIVE_COMMENT_SIZE = 0xFFFF
# Zip64 restates the same fields in wider types. Its record and locator sit
# immediately before the classic record, in that order, and zipfile prefers
# them whenever both are present -- not only when the classic fields overflow.
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_SIZE = 56
_ZIP64_EOCD_DIRECTORY_SIZE_OFFSET = 40
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20


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


def _stream_size(f) -> int:
    """Byte length of an open seekable archive, leaving the position as found.

    Deliberately seek/tell rather than ``os.fstat``: the caller may hand over a
    file it has been writing to, and seeking flushes Python's write buffer
    while ``fstat`` would report the shorter length already on disk.
    """
    pos = f.tell()
    try:
        f.seek(0, os.SEEK_END)
        return f.tell()
    finally:
        f.seek(pos)


def _declared_directory_size(f, size: int) -> int:
    """Bytes the archive claims its central directory occupies.

    This is the value that decides how much work ``zipfile.ZipFile()`` does:
    it walks exactly this many bytes of directory headers, building a
    ``ZipInfo`` for each. Resolved the same way ``zipfile`` resolves it, Zip64
    records included, which is the point -- reading only the classic record
    would let an archive declare one size here and hand ``zipfile`` another.

    Returns 0 when no directory record can be found, in which case ``zipfile``
    will reject the input as not a zip archive.
    """
    window = min(
        size,
        _EOCD_SIZE + _MAX_ARCHIVE_COMMENT_SIZE + _ZIP64_LOCATOR_SIZE + _ZIP64_EOCD_SIZE,
    )
    pos = f.tell()
    try:
        f.seek(size - window)
        tail = f.read(window)
    finally:
        f.seek(pos)
    # zipfile takes the last signature in a window of this width, so taking
    # anything else would be reading a different archive than it will.
    start = tail.rfind(_EOCD_SIGNATURE)
    if start < 0 or len(tail) - start < _EOCD_SIZE:
        return 0

    locator = start - _ZIP64_LOCATOR_SIZE
    record = locator - _ZIP64_EOCD_SIZE
    if (
        record >= 0
        and tail[locator : locator + 4] == _ZIP64_LOCATOR_SIGNATURE
        and tail[record : record + 4] == _ZIP64_EOCD_SIGNATURE
    ):
        (declared,) = struct.unpack_from(
            "<Q", tail, record + _ZIP64_EOCD_DIRECTORY_SIZE_OFFSET
        )
        return declared

    # 0xFFFFFFFF here means "see the Zip64 record", and reaching this line
    # means there is not one to see. It reads as 4 GiB, past any budget worth
    # honouring, which is the right answer for an archive in that state.
    (declared,) = struct.unpack_from("<I", tail, start + _EOCD_DIRECTORY_SIZE_OFFSET)
    return declared


def _reject_oversized_input(f, max_total_bytes, max_members):
    """Refuse an archive before ``zipfile`` reads its directory into memory.

    Constructing a ``ZipFile`` is not free: it reads the whole central
    directory and builds a ``ZipInfo`` per entry plus a name-keyed dict of
    them. Measured on CPython 3.14 that costs roughly 540 bytes of resident
    memory per entry against about 88 bytes of archive, so an archive that is
    almost all directory turns a bounded upload into an unbounded allocation.
    Every limit this module enforces runs after that point, which is too late,
    hence these two checks -- both of which read a fixed number of bytes and
    keep nothing.
    """
    size = _stream_size(f)
    if size > max_total_bytes:
        # Refusing to expand an archive that is already bigger than the most
        # we are willing to write costs nothing real: compression does not
        # make a legitimate bundle grow.
        raise ZipExtractionLimitExceeded(
            f"archive is {size} bytes before expansion, over the "
            f"{max_total_bytes} byte extraction size limit"
        )

    directory_size = _declared_directory_size(f, size)
    directory_budget = max_members * _DIRECTORY_BYTES_PER_MEMBER
    if directory_size > directory_budget:
        raise ZipExtractionLimitExceeded(
            f"archive directory is {directory_size} bytes, over the "
            f"{directory_budget} bytes {max_members} entries can occupy"
        )


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
        # makedirs can fail partway up, having already created the levels
        # above the one that failed. Record whichever now exist so cleanup
        # still takes them back out.
        created_dirs.extend(level for level in missing if os.path.isdir(level))
        return
    created_dirs.extend(missing)


def _remove_partial_output(created_files, created_dirs):
    """Undo the writes of an aborted extraction.

    An archive rejected halfway through has already put bytes on disk; leaving
    them behind would defeat the size limit it was rejected by. Only paths this
    call brought into existence are listed, so a destination that already held
    files keeps them.
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


def _is_extracted(member, prefix):
    """Whether extraction writes this entry, rather than skipping it.

    Anything the loop below skips is also left out of the byte accounting: an
    archive whose real output is two bytes must not be refused for entries
    that never reach the disk.
    """
    if member.endswith("/"):
        return False
    return member.startswith(prefix) and not is_unsafe_path(member)


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
    corresponding Django settings, and both are also applied to the archive
    itself before it is opened, since reading a zip's directory costs memory
    in proportion to how many entries it declares.

    Breaching either raises ``ZipExtractionLimitExceeded`` and removes
    everything this call created, so a rejected archive leaves nothing new
    behind. Files that were already in ``path`` are never removed, though an
    archive member of the same name does overwrite one before the abort.

    Passing an already-open ``zipfile.ZipFile`` skips the pre-open checks:
    whoever opened it has paid that cost already.
    """
    if max_total_bytes is None:
        max_total_bytes = settings.GLITCHTIP_MAX_UNZIPPED_ARCHIVE_SIZE
    if max_members is None:
        max_members = settings.GLITCHTIP_MAX_ARCHIVE_MEMBERS

    close = False
    if isinstance(f, zipfile.ZipFile):
        zf = f
    elif isinstance(f, str):
        close = True
        with open(f, "rb") as fh:
            _reject_oversized_input(fh, max_total_bytes, max_members)
        zf = zipfile.ZipFile(f, "r")
    else:
        _reject_oversized_input(f, max_total_bytes, max_members)
        zf = zipfile.ZipFile(f, "r")

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

        if strip_toplevel:
            prefix = find_common_prefix(members)
        else:
            prefix = ""

        # Cheap early reject for an archive that admits up front it is too
        # big, so an obvious bomb costs no disk writes at all. Advisory only:
        # these are declared sizes, and the per-member count below is what
        # actually holds the line.
        declared = sum(
            info.file_size
            for info in zf.infolist()
            if _is_extracted(info.filename, prefix)
        )
        if declared > max_total_bytes:
            raise ZipExtractionLimitExceeded(
                "archive declares more than the extraction size limit"
            )

        # The destination belongs to the caller: bring it into existence
        # without recording it, so an abort never rmdirs it out from under
        # them, and so no level at or above it is ever a cleanup candidate.
        os.makedirs(path, exist_ok=True)
        remaining = max_total_bytes
        for member in members:
            if not _is_extracted(member, prefix):
                continue
            dst_path = os.path.join(path, member[len(prefix) :])
            _makedirs_tracked(os.path.dirname(dst_path), created_dirs)
            if not os.path.exists(dst_path):
                created_files.append(dst_path)
            remaining -= _extract_member(zf, member, dst_path, remaining)
    except BaseException:
        _remove_partial_output(created_files, created_dirs)
        raise
    finally:
        if close:
            zf.close()

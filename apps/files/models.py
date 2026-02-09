import mmap
import tempfile
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1

from asgiref.sync import sync_to_async
from django.core.files.base import File as FileObj
from django.db import models, transaction

from glitchtip.base_models import CreatedModel

from .exceptions import AssembleChecksumMismatch


def _get_size_and_checksum(fileobj):
    size = 0
    checksum = sha1()
    while True:
        chunk = fileobj.read(65536)
        if not chunk:
            break
        size += len(chunk)
        checksum.update(chunk)
    return size, checksum.hexdigest()


class FileBlob(CreatedModel):
    """
    Port of sentry.models.file.FileBlob with simplifications

    OSS Sentry stores files in file blob chunks. Where one file gets saved as many blobs.
    GlitchTip uses Django FileField and does not split files into chunks.
    The FileBlob's provide file deduplication. Multiple File objects may refer to the same
    FileBlob.
    """

    blob = models.FileField(upload_to="uploads/file_blobs")
    size = models.PositiveIntegerField(null=True)
    checksum = models.CharField(max_length=40, unique=True)

    @classmethod
    async def from_files(cls, files, organization=None, logger=None):
        logger.debug("FileBlob.from_files.start")

        files_with_checksums = []
        for fileobj in files:
            if isinstance(fileobj, tuple):
                files_with_checksums.append(fileobj)
            else:
                files_with_checksums.append((fileobj, None))

        for file_with_checksum in files_with_checksums:
            blob = cls()
            blob_file = file_with_checksum[0]
            blob.size = file_with_checksum[0].size
            blob.checksum = file_with_checksum[1]
            if not await FileBlob.objects.filter(checksum=blob.checksum).aexists():
                await sync_to_async(blob.blob.save)(blob_file.name, blob_file)

    @classmethod
    def from_file(cls, fileobj):
        """
        Retrieve a single FileBlob instances for the given file.
        """
        checksum = sha1()
        with fileobj.open("rb") as f:
            if f.multiple_chunks():
                for chunk in f.chunks():
                    checksum.update(chunk)
            else:
                checksum.update(f.read())
            # Significant deviation from OSS Sentry
            file_blob, _ = cls.objects.get_or_create(
                checksum=checksum.hexdigest(),
                defaults={"blob": fileobj, "size": fileobj.size},
            )
        return file_blob


class File(CreatedModel):
    """
    Port of sentry.models.file.File
    """

    name = models.TextField()
    # last_used = models.DateTimeField(auto_now=True, db_index=True)
    # debug_id = models.UUIDField(
    #     null=True,
    #     blank=True,
    #     db_index=True,
    #     help_text="Association between file and source code",
    # )  # Remove this?
    type = models.CharField(max_length=64)  # Not currently used
    headers = models.JSONField(blank=True, null=True)
    blob = models.ForeignKey(FileBlob, on_delete=models.CASCADE, null=True)
    size = models.PositiveIntegerField(default=0)  # Not currently used
    checksum = models.CharField(max_length=40, null=True, db_index=True)

    def put_django_file(self, fileobj):
        """Save a Django File object as a File Blob"""
        self.size = fileobj.size
        file_blob = FileBlob.from_file(fileobj)
        self.checksum = file_blob.checksum
        self.save()

    def putfile(self, fileobj):
        """Save a file-like object as a File Blob"""
        size, checksum = _get_size_and_checksum(fileobj)
        fileobj.seek(0)
        file_blob, _ = FileBlob.objects.get_or_create(
            defaults={"blob": FileObj(fileobj, name=checksum)},
            size=size,
            checksum=checksum,
        )
        self.checksum = checksum
        self.blob = file_blob
        self.save()

    def _get_chunked_blob(
        self, mode=None, prefetch=False, prefetch_to=None, delete=True
    ):
        return ChunkedFileBlobIndexWrapper(
            FileBlobIndex.objects.filter(file=self)
            .select_related("blob")
            .order_by("offset"),
            mode=mode,
            prefetch=prefetch,
            prefetch_to=prefetch_to,
            delete=delete,
        )

    def getfile(self):
        impl = self._get_chunked_blob()
        return FileObj(impl, self.name)

    def assemble_from_file_blob_ids(self, file_blob_ids, checksum, commit=True):
        """
        This creates a file, from file blobs and returns a temp file with the
        contents.
        """
        tf = tempfile.NamedTemporaryFile()
        with transaction.atomic():
            file_blobs = FileBlob.objects.filter(id__in=file_blob_ids).all()

            # Ensure blobs are in the order and duplication as provided
            blobs_by_id = {blob.id: blob for blob in file_blobs}
            file_blobs = [blobs_by_id[blob_id] for blob_id in file_blob_ids]

            new_checksum = sha1(b"")
            offset = 0
            for blob in file_blobs:
                FileBlobIndex.objects.create(file=self, blob=blob, offset=offset)
                for chunk in blob.blob.chunks():
                    new_checksum.update(chunk)
                    tf.write(chunk)
                offset += blob.size

            self.size = offset
            self.checksum = new_checksum.hexdigest()

            if checksum != self.checksum:
                raise AssembleChecksumMismatch("Checksum mismatch")

        if commit:
            self.save()
        tf.flush()
        tf.seek(0)
        return tf


class FileBlobIndex(models.Model):
    """
    Ported from OSS Sentry. Should be removed as GlitchTip does not
    split file blobs into chunks.
    """

    file = models.ForeignKey(File, on_delete=models.CASCADE)
    blob = models.ForeignKey(FileBlob, on_delete=models.CASCADE)
    offset = models.PositiveIntegerField()

    class Meta:
        unique_together = (("file", "blob", "offset"),)


class ChunkedFileBlobIndexWrapper(object):
    def __init__(
        self, indexes, mode=None, prefetch=False, prefetch_to=None, delete=True
    ):
        # eager load from database incase its a queryset
        self._indexes = list(indexes)
        self._curfile = None
        self._curidx = None
        if prefetch:
            self.prefetched = True
            self._prefetch(prefetch_to, delete)
        else:
            self.prefetched = False
        self.mode = mode
        self.open()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.close()

    def detach_tempfile(self):
        if not self.prefetched:
            raise TypeError("Can only detech tempfiles in prefetch mode")
        rv = self._curfile
        self._curfile = None
        self.close()
        rv.seek(0)
        return rv

    def _nextidx(self):
        assert not self.prefetched, "this makes no sense"
        old_file = self._curfile
        try:
            try:
                self._curidx = next(self._idxiter)
                self._curfile = self._curidx.blob.getfile()
            except StopIteration:
                self._curidx = None
                self._curfile = None
        finally:
            if old_file is not None:
                old_file.close()

    @property
    def size(self):
        return sum(i.blob.size for i in self._indexes)

    def open(self):
        self.closed = False
        self.seek(0)

    def _prefetch(self, prefetch_to=None, delete=True):
        size = self.size
        f = tempfile.NamedTemporaryFile(
            prefix="._prefetch-", dir=prefetch_to, delete=delete
        )
        if size == 0:
            self._curfile = f
            return

        # Zero out the file
        f.seek(size - 1)
        f.write("\x00")
        f.flush()

        mem = mmap.mmap(f.fileno(), size)

        def fetch_file(offset, getfile):
            with getfile() as sf:
                while True:
                    chunk = sf.read(65535)
                    if not chunk:
                        break
                    mem[offset : offset + len(chunk)] = chunk
                    offset += len(chunk)

        with ThreadPoolExecutor(max_workers=4) as exe:
            for idx in self._indexes:
                exe.submit(fetch_file, idx.offset, idx.blob.getfile)

        mem.flush()
        self._curfile = f

    def close(self):
        if self._curfile:
            self._curfile.close()
        self._curfile = None
        self._curidx = None
        self.closed = True

    def seek(self, pos):
        if self.closed:
            raise ValueError("I/O operation on closed file")

        if self.prefetched:
            return self._curfile.seek(pos)

        if pos < 0:
            raise IOError("Invalid argument")
        for n, idx in enumerate(self._indexes[::-1]):
            if idx.offset <= pos:
                if idx != self._curidx:
                    self._idxiter = iter(self._indexes[-(n + 1) :])
                    self._nextidx()
                break
        else:
            raise ValueError("Cannot seek to pos")
        self._curfile.seek(pos - self._curidx.offset)

    def tell(self):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if self.prefetched:
            return self._curfile.tell()
        if self._curfile is None:
            return self.size
        return self._curidx.offset + self._curfile.tell()

    def read(self, n=-1):
        if self.closed:
            raise ValueError("I/O operation on closed file")

        if self.prefetched:
            return self._curfile.read(n)

        result = bytearray()

        # Read to the end of the file
        if n < 0:
            while self._curfile is not None:
                blob_result = self._curfile.read(32768)
                if not blob_result:
                    self._nextidx()
                else:
                    result.extend(blob_result)

        # Read until a certain number of bytes are read
        else:
            while n > 0 and self._curfile is not None:
                blob_result = self._curfile.read(min(n, 32768))
                if not blob_result:
                    self._nextidx()
                else:
                    n -= len(blob_result)
                    result.extend(blob_result)

        return bytes(result)

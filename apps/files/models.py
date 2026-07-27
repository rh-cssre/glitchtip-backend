import tempfile
from hashlib import sha1

from django.core.files.base import File as FileObj
from django.db import models

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
        if logger:
            logger.debug("FileBlob.from_files.start")

        for fileobj in files:
            if isinstance(fileobj, tuple):
                blob_file, checksum = fileobj
            else:
                blob_file, checksum = fileobj, None

            await cls.objects.aget_or_create(
                checksum=checksum,
                defaults={
                    "blob": FileObj(blob_file, name=blob_file.name),
                    "size": blob_file.size,
                },
            )

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

    def assemble_from_file_blob_ids(self, file_blob_ids, checksum, commit=True):
        """
        This creates a file, from file blobs and returns a temp file with the
        contents.
        """
        tf = tempfile.NamedTemporaryFile()
        file_blobs = FileBlob.objects.filter(id__in=file_blob_ids).all()

        # Ensure blobs are in the order and duplication as provided
        blobs_by_id = {blob.id: blob for blob in file_blobs}
        file_blobs = [blobs_by_id[blob_id] for blob_id in file_blob_ids]

        new_checksum = sha1(b"")
        offset = 0
        for blob in file_blobs:
            for chunk in blob.blob.chunks():
                new_checksum.update(chunk)
                tf.write(chunk)
            offset += blob.size

        self.size = offset
        self.checksum = new_checksum.hexdigest()

        if checksum != self.checksum:
            raise AssembleChecksumMismatch("Checksum mismatch")

        if len(file_blobs) == 1:
            self.blob = file_blobs[0]
        elif file_blobs:
            tf.flush()
            tf.seek(0)
            combined_blob, _ = FileBlob.objects.get_or_create(
                checksum=self.checksum,
                defaults={"blob": FileObj(tf, name=self.checksum), "size": offset},
            )
            self.blob = combined_blob
        else:
            self.blob = None

        if commit:
            self.save()
        tf.flush()
        tf.seek(0)
        return tf

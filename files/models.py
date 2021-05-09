from hashlib import sha1
from django.db import models
from glitchtip.base_models import CreatedModel


class FileBlob(CreatedModel):
    """
    Port of sentry.models.file.FileBlog with some simplifications
    """

    blob = models.FileField(upload_to="uploads/file_blobs")
    size = models.PositiveIntegerField(null=True)
    checksum = models.CharField(max_length=40, unique=True)

    @classmethod
    def from_files(cls, files, organization=None, logger=None):
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
            blob.checksum = file_with_checksum[1]
            blob.blob.save(blob_file.name, blob_file)
            blob.save()

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
    headers = models.JSONField()
    blobs = models.ManyToManyField(FileBlob)
    size = models.PositiveIntegerField(null=True)
    checksum = models.CharField(max_length=40, null=True, db_index=True)

    def putfile(self, fileobj):
        self.size = fileobj.size
        file_blob = FileBlob.from_file(fileobj)
        self.checksum = file_blob.checksum
        self.save()


from django.db import models
from glitchtip.base_models import CreatedModel


class FileBlob(CreatedModel):
    upload = models.FileField(upload_to="uploads/")
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
            blob.upload.save(blob_file.name, blob_file)
            blob.save()

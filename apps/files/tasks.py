from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from apps.organizations_ext.models import Organization

from .assemble import assemble_artifacts
from .models import FileBlob


@shared_task
def assemble_artifacts_task(org_id, version, checksum, chunks, **kwargs):
    """
    Creates release files from an uploaded artifact bundle.
    """
    organization = Organization.objects.get(pk=org_id)
    assemble_artifacts(organization, version, checksum, chunks)


def cleanup_old_files():
    """
    Delete files in both the database and media storage

    Deletion only occurs when the creation date of the following are older than max file life days
    - FileBlob
    - Any related File objects
    - Any related events attached to the project, release, release file

    This operation has minor risk of deleting a file that is still desired,
    if it hasn't been used for a long time and have no recent event data
    """
    days_ago = now() - timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS)

    while True:
        file_blobs = (
            FileBlob.objects.filter(created__lt=days_ago)
            .exclude(file__created__gte=days_ago)
            .exclude(
                file__debugsymbolbundle__release__projects__issues__last_seen__gte=days_ago
            )
        ).only("id", "blob")[:1000]
        ids = []
        for file_blob in file_blobs:
            ids.append(file_blob.id)
            file_blob.blob.delete()  # Delete actual file
        if ids:
            FileBlob.objects.filter(id__in=ids).delete()
        else:
            break

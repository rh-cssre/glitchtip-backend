from celery import shared_task
from organizations_ext.models import Organization
from .assemble import assemble_artifacts


@shared_task
def assemble_artifacts_task(org_id, version, checksum, chunks, **kwargs):
    """
    Creates release files from an uploaded artifact bundle.
    """
    organization = Organization.objects.get(pk=org_id)
    assemble_artifacts(organization, version, checksum, chunks)

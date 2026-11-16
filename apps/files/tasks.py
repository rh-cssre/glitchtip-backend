from asgiref.sync import sync_to_async
from django.tasks import task

from apps.organizations_ext.models import Organization

from .assemble import assemble_artifacts


@task
async def assemble_artifacts_task(org_id, version, checksum, chunks, **kwargs):
    """
    Creates release files from an uploaded artifact bundle.
    """
    organization = await Organization.objects.aget(pk=org_id)
    await sync_to_async(assemble_artifacts)(organization, version, checksum, chunks)

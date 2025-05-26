from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission
from glitchtip.utils import async_call_celery_task

from .schema import ArtifactBundleAssembleIn

router = Router()


@router.post("organizations/{slug:organization_slug}/artifactbundle/assemble/")
@has_permission(["project:write", "project:admin", "project:releases"])
async def artifact_bundle_assemble(
    request: AuthHttpRequest, organization_slug: str, payload: ArtifactBundleAssembleIn
):
    """Associate files with assembly bundle and optionally release"""
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )

    await async_call_celery_task(
        assemble_artifacts_task,
        organization.id,
        payload.version,
        payload.checksum,
        payload.chunks,
    )
    return {"state": "created", "missingChunks": []}

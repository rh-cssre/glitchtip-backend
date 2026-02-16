from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.files.models import FileBlob
from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import has_permission

from .schema import ArtifactBundleAssembleIn

router = Router()


@optional_slash(
    router, "post", "organizations/{slug:organization_slug}/artifactbundle/assemble/"
)
@has_permission(["project:write", "project:admin", "project:releases"])
async def artifact_bundle_assemble(
    request: AuthHttpRequest, organization_slug: str, payload: ArtifactBundleAssembleIn
):
    """Associate files with assembly bundle and optionally release"""
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )

    existing_chunks = [
        checksum
        async for checksum in FileBlob.objects.filter(
            checksum__in=payload.chunks
        ).values_list("checksum", flat=True)
    ]
    missing_chunks = list(set(payload.chunks) - set(existing_chunks))

    if missing_chunks:
        return {"state": "not_found", "missingChunks": missing_chunks}

    await assemble_artifacts_task.aenqueue(
        organization.id,
        payload.version,
        payload.checksum,
        payload.chunks,
    )
    return {"state": "created", "missingChunks": []}

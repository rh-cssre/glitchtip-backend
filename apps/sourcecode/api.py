from django.db import IntegrityError
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import HttpError

from apps.files.models import FileBlob
from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    org_scoped,
    org_scoped_list,
)

from .models import Repository
from .schema import ArtifactBundleAssembleIn, RepositoryIn, RepositorySchema

router = Router()


@router.get(
    "organizations/{slug:organization_id_or_slug}/repos/",
    response=list[RepositorySchema],
    by_alias=True,
)
@org_scoped_list(["org:read", "org:write", "org:admin"], Repository)
async def list_repositories(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
):
    return Repository.objects.order_by("-created")


@router.post(
    "organizations/{slug:organization_id_or_slug}/repos/",
    response={201: RepositorySchema},
    by_alias=True,
)
@org_scoped(["org:write", "org:admin"], Organization)
async def create_repository(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: RepositoryIn,
):
    organization = await aget_object_or_404(qs)
    try:
        repo = await Repository.objects.acreate(
            organization=organization,
            name=payload.name,
            url=payload.url,
            provider=payload.provider or {},
        )
    except IntegrityError:
        raise HttpError(409, "A repository with this name already exists.")
    return Status(201, repo)


@optional_slash(
    router,
    "post",
    "organizations/{slug:organization_id_or_slug}/artifactbundle/assemble/",
)
@org_scoped(["project:write", "project:admin", "project:releases"], Organization)
async def artifact_bundle_assemble(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: ArtifactBundleAssembleIn,
):
    """Associate files with assembly bundle and optionally release"""
    organization = await aget_object_or_404(qs.only("id"))

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

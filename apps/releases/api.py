from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import ValidationError
from ninja.pagination import paginate

from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from apps.sourcecode.models import DebugSymbolBundle
from apps.sourcecode.schema import DebugSymbolBundleSchema
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission
from glitchtip.utils import async_call_celery_task

from .models import Release
from .schema import (
    AssembleSchema,
    ReleaseBase,
    ReleaseIn,
    ReleaseSchema,
    ReleaseUpdate,
)

router = Router()


"""
POST /organizations/{organization_slug}/releases/
POST /organizations/{organization_slug}/releases/{version}/deploys/ (Not implemented)
GET /organizations/{organization_slug}/releases/
GET /organizations/{organization_slug}/releases/{version}/
PUT /organizations/{organization_slug}/releases/{version}/
DELETE /organizations/{organization_slug}/releases/{version}/
GET /organizations/{organization_slug}/releases/{version}/files/
GET /organizations/{organization_slug}/releases/{version}/files/{file_id}/
POST /organizations/{organization_slug}/releases/{version}/assemble/ (sentry undocumented)
DELETE /organizations/{organization_slug}/releases/{version}/files/{file_id}/
GET /projects/{organization_slug}/{project_slug}/releases/ (sentry undocumented)
GET /projects/{organization_slug}/{project_slug}/releases/{version}/ (sentry undocumented)
DELETE /projects/{organization_slug}/{project_slug}/releases/{version}/ (sentry undocumented)
PUT /projects/organizations/{organization_slug}/releases/{version}/ (sentry undocumented)
POST /projects/{organization_slug}/{project_slug}/releases/ (sentry undocumented)
GET /projects/{organization_slug}/{project_slug}/releases/{version}/files/{file_id}/
DELETE /projects/{organization_slug}/{project_slug}/releases/{version}/files/{file_id}/ (sentry undocumented)
"""


def get_releases_queryset(
    organization_slug: str,
    user_id: int,
    id: int | None = None,
    version: str | None = None,
    project_slug: str | None = None,
):
    qs = Release.objects.filter(
        organization__slug=organization_slug, organization__users=user_id
    )
    if id:
        qs = qs.filter(id=id)
    if version:
        qs = qs.filter(version=version)
    if project_slug:
        qs = qs.filter(projects__slug=project_slug)
    return qs.prefetch_related("projects")


def get_release_files_queryset(
    organization_slug: str,
    user_id: int,
    version: str | None = None,
    project_slug: str | None = None,
    id: int | None = None,
):
    qs = DebugSymbolBundle.objects.filter(
        release__organization__slug=organization_slug,
        release__organization__users=user_id,
    )
    if id:
        qs = qs.filter(id=id)
    if version:
        qs = qs.filter(release__version=version)
    if project_slug:
        qs = qs.filter(release__projects__slug=project_slug)
    return qs.select_related("file")


@router.post(
    "/organizations/{slug:organization_slug}/releases/",
    response={201: ReleaseSchema},
    by_alias=True,
)
@has_permission(["project:releases"])
async def create_release(
    request: AuthHttpRequest, organization_slug: str, payload: ReleaseIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )
    data = payload.dict()
    projects = [
        project_id
        async for project_id in Project.objects.filter(
            slug__in=data.pop("projects"), organization=organization
        ).values_list("id", flat=True)
    ]
    if not projects:
        raise ValidationError([{"projects": "Require at least one valid project"}])
    release = await Release.objects.acreate(organization=organization, **data)
    await release.projects.aadd(*projects)
    return await get_releases_queryset(organization_slug, user_id, id=release.id).aget()


@router.post(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/",
    response={201: ReleaseSchema},
    by_alias=True,
)
@has_permission(["project:releases"])
async def create_project_release(
    request: AuthHttpRequest, organization_slug: str, project_slug, payload: ReleaseBase
):
    user_id = request.auth.user_id
    project = await aget_object_or_404(
        Project.objects.select_related("organization"),
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=user_id,
    )
    data = payload.dict()
    version = data.pop("version")
    release, _ = await Release.objects.aget_or_create(
        organization=project.organization, version=version, defaults=data
    )
    await release.projects.aadd(project)
    return await get_releases_queryset(organization_slug, user_id, id=release.id).aget()


@router.get(
    "/organizations/{slug:organization_slug}/releases/",
    response=list[ReleaseSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:releases"])
async def list_releases(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_releases_queryset(organization_slug, request.auth.user_id)


@router.get(
    "/organizations/{slug:organization_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def get_release(request: AuthHttpRequest, organization_slug: str, version: str):
    return await aget_object_or_404(
        get_releases_queryset(organization_slug, request.auth.user_id, version=version)
    )


@router.put(
    "/organizations/{slug:organization_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def update_release(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
    payload: ReleaseUpdate,
):
    user_id = request.auth.user_id
    release = await aget_object_or_404(
        get_releases_queryset(organization_slug, user_id, version=version)
    )
    for attr, value in payload.dict().items():
        setattr(release, attr, value)
    await release.asave()
    return await get_releases_queryset(organization_slug, user_id, id=release.id).aget()


@router.delete(
    "/organizations/{slug:organization_slug}/releases/{str:version}/",
    response={204: None},
)
@has_permission(["project:releases"])
async def delete_organization_release(
    request: AuthHttpRequest, organization_slug: str, version: str
):
    result, _ = await get_releases_queryset(
        organization_slug, request.auth.user_id, version=version
    ).adelete()
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/organizations/{slug:organization_slug}/releases/{str:version}/files/",
    response=list[DebugSymbolBundleSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:releases"])
async def list_release_files(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    version: str,
):
    return get_release_files_queryset(
        organization_slug,
        request.auth.user_id,
        version=version,
    )


@router.get(
    "/organizations/{slug:organization_slug}/releases/{str:version}/files/{int:file_id}/",
    response=DebugSymbolBundleSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def get_organization_release_file(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    return await aget_object_or_404(
        get_release_files_queryset(
            organization_slug,
            request.auth.user_id,
            project_slug=project_slug,
            version=version,
            id=file_id,
        )
    )


@router.delete(
    "/organizations/{slug:organization_slug}/releases/{str:version}/files/{int:file_id}/",
    response={204: None},
)
@has_permission(["project:releases"])
async def delete_organization_release_file(
    request: AuthHttpRequest, organization_slug: str, version: str, file_id: int
):
    result, _ = await get_release_files_queryset(
        organization_slug, request.auth.user_id, version=version, id=file_id
    ).adelete()
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/",
    response=list[ReleaseSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:releases"])
async def list_project_releases(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_releases_queryset(
        organization_slug, request.auth.user_id, project_slug=project_slug
    )


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def get_project_release(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, version: str
):
    return await aget_object_or_404(
        get_releases_queryset(
            organization_slug,
            request.auth.user_id,
            project_slug=project_slug,
            version=version,
        )
    )


@router.put(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def update_project_release(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    version: str,
    payload: ReleaseUpdate,
):
    user_id = request.auth.user_id
    release = await aget_object_or_404(
        get_releases_queryset(
            organization_slug, user_id, version=version, project_slug=project_slug
        )
    )
    for attr, value in payload.dict().items():
        setattr(release, attr, value)
    await release.asave()
    return await get_releases_queryset(
        organization_slug, user_id, id=release.id, project_slug=project_slug
    ).aget()


@router.delete(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/",
    response={204: None},
)
@has_permission(["project:releases"])
async def delete_project_release(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, version: str
):
    result, _ = await get_releases_queryset(
        organization_slug,
        request.auth.user_id,
        version=version,
        project_slug=project_slug,
    ).adelete()
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/files/",
    response=list[DebugSymbolBundleSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:releases"])
async def list_project_release_files(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
    version: str,
):
    return get_release_files_queryset(
        organization_slug,
        request.auth.user_id,
        project_slug=project_slug,
        version=version,
    )


@router.delete(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/files/{int:file_id}/",
    response={204: None},
)
@has_permission(["project:releases"])
async def delete_project_release_file(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    result, _ = await get_release_files_queryset(
        organization_slug,
        request.auth.user_id,
        version=version,
        id=file_id,
        project_slug=project_slug,
    ).adelete()
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/releases/{str:version}/files/{int:file_id}/",
    response=DebugSymbolBundleSchema,
    by_alias=True,
)
@has_permission(["project:releases"])
async def get_project_release_file(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    return await aget_object_or_404(
        get_release_files_queryset(
            organization_slug,
            request.auth.user_id,
            project_slug=project_slug,
            version=version,
            id=file_id,
        )
    )


@router.post("/organizations/{slug:organization_slug}/releases/{str:version}/assemble/")
@has_permission(["project:releases", "project:write", "project:admin"])
async def assemble_release(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
    payload: AssembleSchema,
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )

    await async_call_celery_task(
        assemble_artifacts_task,
        organization.id,
        version,
        payload.checksum,
        payload.chunks,
    )

    # TODO should return more state's
    return {"state": "ok", "missingChunks": []}

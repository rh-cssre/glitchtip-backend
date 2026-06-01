from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import ValidationError
from ninja.pagination import paginate

from apps.files.models import FileBlob
from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from apps.sourcecode.models import DebugSymbolBundle
from apps.sourcecode.schema import DebugSymbolBundleSchema
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import has_permission

from .models import Deploy, Release
from .schema import (
    AssembleSchema,
    CommitIn,
    CommitSchema,
    DeployIn,
    DeploySchema,
    ReleaseBase,
    ReleaseIn,
    ReleaseSchema,
    ReleaseUpdate,
)

router = Router()


"""
POST /organizations/{organization_slug}/releases/
POST /organizations/{organization_slug}/releases/{version}/deploys/
GET /organizations/{organization_slug}/releases/{version}/deploys/
POST /organizations/{organization_slug}/releases/{version}/commits/
GET /organizations/{organization_slug}/releases/{version}/commits/
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
    return qs.select_related("repository").prefetch_related("projects")


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
    project_slugs = data.pop("projects")
    projects = [
        project_id
        async for project_id in Project.objects.filter(
            slug__in=project_slugs, organization=organization
        ).values_list("id", flat=True)
    ]
    if not projects:
        raise ValidationError([{"projects": "Require at least one valid project"}])
    version = data.pop("version")
    release, _ = await Release.objects.aget_or_create(
        organization=organization, version=version, defaults=data
    )
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
    return Status(204, None)


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
    return Status(204, None)


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
    return Status(204, None)


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
    return Status(204, None)


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


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_slug}/releases/{str:version}/deploys/",
    response={201: DeploySchema},
    by_alias=True,
)
@has_permission(["project:releases", "project:write", "project:admin"])
async def create_deploy(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
    payload: DeployIn,
):
    user_id = request.auth.user_id
    release = await aget_object_or_404(
        get_releases_queryset(organization_slug, user_id, version=version)
    )
    deploy = await Deploy.objects.acreate(
        release=release,
        environment=payload.environment,
        url=payload.url,
        date_started=payload.date_started,
        date_finished=payload.date_finished,
    )
    await Release.objects.filter(id=release.id).aupdate(
        deploy_count=F("deploy_count") + 1
    )
    return Status(201, deploy)


@optional_slash(
    router,
    "get",
    "/organizations/{slug:organization_slug}/releases/{str:version}/deploys/",
    response=list[DeploySchema],
    by_alias=True,
)
@has_permission(["project:releases", "project:write", "project:admin"])
async def list_deploys(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
):
    release = await aget_object_or_404(
        get_releases_queryset(organization_slug, request.auth.user_id, version=version)
    )
    return [deploy async for deploy in Deploy.objects.filter(release=release)]


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_slug}/releases/{str:version}/commits/",
    response=ReleaseSchema,
    by_alias=True,
)
@has_permission(["project:releases", "project:write", "project:admin"])
async def create_commits(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
    payload: list[CommitIn],
):
    user_id = request.auth.user_id
    release = await aget_object_or_404(
        get_releases_queryset(organization_slug, user_id, version=version)
    )
    commits = [commit.dict(by_alias=True) for commit in payload]
    release.commit_count = len(commits)
    release.data["commits"] = commits[:1000]
    await release.asave(update_fields=["commit_count", "data"])
    return await get_releases_queryset(organization_slug, user_id, id=release.id).aget()


@optional_slash(
    router,
    "get",
    "/organizations/{slug:organization_slug}/releases/{str:version}/commits/",
    response=list[CommitSchema],
    by_alias=True,
)
@has_permission(["project:releases", "project:write", "project:admin"])
async def list_commits(
    request: AuthHttpRequest,
    organization_slug: str,
    version: str,
):
    release = await aget_object_or_404(
        get_releases_queryset(organization_slug, request.auth.user_id, version=version)
    )
    return release.data.get("commits", [])


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_slug}/releases/{str:version}/assemble/",
)
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
        version,
        payload.checksum,
        payload.chunks,
    )

    return {"state": "created", "missingChunks": []}

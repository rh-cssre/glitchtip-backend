from django.db.models import F, QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import ValidationError

from apps.files.models import FileBlob
from apps.files.tasks import assemble_artifacts_task
from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from apps.sourcecode.models import DebugSymbolBundle
from apps.sourcecode.schema import DebugSymbolBundleSchema
from glitchtip.api.decorators import optional_slash
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    org_scoped,
    org_scoped_list,
)

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


def with_release_details(qs: QuerySet[Release]) -> QuerySet[Release]:
    return qs.select_related("repository").prefetch_related("projects")


async def get_release_details(release_id: int) -> Release:
    return await with_release_details(Release.objects).aget(id=release_id)


@router.post(
    "/organizations/{slug:organization_id_or_slug}/releases/",
    response={201: ReleaseSchema},
    by_alias=True,
)
@org_scoped(["project:releases"], Organization)
async def create_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: ReleaseIn,
):
    organization = await aget_object_or_404(qs)
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
    return await get_release_details(release.id)


@router.post(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/",
    response={201: ReleaseSchema},
    by_alias=True,
)
@org_scoped(["project:releases"], Project)
async def create_project_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug,
    payload: ReleaseBase,
):
    project = await aget_object_or_404(qs, slug=project_slug)
    data = payload.dict()
    version = data.pop("version")
    release, _ = await Release.objects.aget_or_create(
        organization_id=project.organization_id, version=version, defaults=data
    )
    await release.projects.aadd(project)
    return await get_release_details(release.id)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/releases/",
    response=list[ReleaseSchema],
    by_alias=True,
)
@org_scoped_list(["project:releases"], Release)
async def list_releases(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
):
    return with_release_details(Release.objects.all())


@router.get(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], Release)
async def get_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
):
    return await aget_object_or_404(with_release_details(qs), version=version)


@router.put(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], Release)
async def update_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
    payload: ReleaseUpdate,
):
    release = await aget_object_or_404(qs, version=version)
    for attr, value in payload.dict().items():
        setattr(release, attr, value)
    await release.asave()
    return await get_release_details(release.id)


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/",
    response={204: None},
)
@org_scoped(["project:releases"], Release)
async def delete_organization_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
):
    result, _ = await qs.filter(version=version).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/files/",
    response=list[DebugSymbolBundleSchema],
    by_alias=True,
)
@org_scoped_list(["project:releases"], DebugSymbolBundle)
async def list_release_files(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    version: str,
):
    return DebugSymbolBundle.objects.filter(release__version=version).select_related(
        "file"
    )


@router.get(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/files/{int:file_id}/",
    response=DebugSymbolBundleSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], DebugSymbolBundle)
async def get_organization_release_file(
    request: OrgScopedHttpRequest,
    qs: QuerySet[DebugSymbolBundle],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    return await aget_object_or_404(
        qs.select_related("file"),
        release__projects__slug=project_slug,
        release__version=version,
        id=file_id,
    )


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/files/{int:file_id}/",
    response={204: None},
)
@org_scoped(["project:releases"], DebugSymbolBundle)
async def delete_organization_release_file(
    request: OrgScopedHttpRequest,
    qs: QuerySet[DebugSymbolBundle],
    organization_id_or_slug: str,
    version: str,
    file_id: int,
):
    result, _ = await qs.filter(release__version=version, id=file_id).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/",
    response=list[ReleaseSchema],
    by_alias=True,
)
@org_scoped_list(["project:releases"], Release)
async def list_project_releases(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
):
    return with_release_details(Release.objects.filter(projects__slug=project_slug))


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], Release)
async def get_project_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
):
    return await aget_object_or_404(
        with_release_details(qs), projects__slug=project_slug, version=version
    )


@router.put(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/",
    response=ReleaseSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], Release)
async def update_project_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
    payload: ReleaseUpdate,
):
    release = await aget_object_or_404(qs, projects__slug=project_slug, version=version)
    for attr, value in payload.dict().items():
        setattr(release, attr, value)
    await release.asave()
    return await get_release_details(release.id)


@router.delete(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/",
    response={204: None},
)
@org_scoped(["project:releases"], Release)
async def delete_project_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
):
    result, _ = await qs.filter(projects__slug=project_slug, version=version).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/files/",
    response=list[DebugSymbolBundleSchema],
    by_alias=True,
)
@org_scoped_list(["project:releases"], DebugSymbolBundle)
async def list_project_release_files(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
):
    return DebugSymbolBundle.objects.filter(
        release__projects__slug=project_slug, release__version=version
    ).select_related("file")


@router.delete(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/files/{int:file_id}/",
    response={204: None},
)
@org_scoped(["project:releases"], DebugSymbolBundle)
async def delete_project_release_file(
    request: OrgScopedHttpRequest,
    qs: QuerySet[DebugSymbolBundle],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    result, _ = await qs.filter(
        release__projects__slug=project_slug, release__version=version, id=file_id
    ).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/releases/{str:version}/files/{int:file_id}/",
    response=DebugSymbolBundleSchema,
    by_alias=True,
)
@org_scoped(["project:releases"], DebugSymbolBundle)
async def get_project_release_file(
    request: OrgScopedHttpRequest,
    qs: QuerySet[DebugSymbolBundle],
    organization_id_or_slug: str,
    project_slug: str,
    version: str,
    file_id: int,
):
    return await aget_object_or_404(
        qs.select_related("file"),
        release__projects__slug=project_slug,
        release__version=version,
        id=file_id,
    )


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/deploys/",
    response={201: DeploySchema},
    by_alias=True,
)
@org_scoped(["project:releases", "project:write", "project:admin"], Release)
async def create_deploy(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
    payload: DeployIn,
):
    release = await aget_object_or_404(qs.only("id"), version=version)
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
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/deploys/",
    response=list[DeploySchema],
    by_alias=True,
)
@org_scoped(["project:releases", "project:write", "project:admin"], Release)
async def list_deploys(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
):
    release = await aget_object_or_404(qs.only("id"), version=version)
    return [deploy async for deploy in Deploy.objects.filter(release=release)]


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/commits/",
    response=ReleaseSchema,
    by_alias=True,
)
@org_scoped(["project:releases", "project:write", "project:admin"], Release)
async def create_commits(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
    payload: list[CommitIn],
):
    release = await aget_object_or_404(with_release_details(qs), version=version)
    commits = [commit.dict(by_alias=True) for commit in payload]
    release.commit_count = len(commits)
    release.data["commits"] = commits[:1000]
    await release.asave(update_fields=["commit_count", "data"])
    return release


@optional_slash(
    router,
    "get",
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/commits/",
    response=list[CommitSchema],
    by_alias=True,
)
@org_scoped(["project:releases", "project:write", "project:admin"], Release)
async def list_commits(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Release],
    organization_id_or_slug: str,
    version: str,
):
    release = await aget_object_or_404(qs, version=version)
    return release.data.get("commits", [])


@optional_slash(
    router,
    "post",
    "/organizations/{slug:organization_id_or_slug}/releases/{str:version}/assemble/",
)
@org_scoped(["project:releases", "project:write", "project:admin"], Organization)
async def assemble_release(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    version: str,
    payload: AssembleSchema,
):
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
        version,
        payload.checksum,
        payload.chunks,
    )

    return {"state": "created", "missingChunks": []}

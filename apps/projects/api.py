from typing import Optional
from uuid import UUID

from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.pagination import paginate

from apps.organizations_ext.models import Organization, OrganizationUserRole
from apps.teams.models import Team
from apps.teams.schema import ProjectTeamSchema
from glitchtip.api.permissions import AuthHttpRequest, has_permission

from .models import Project, ProjectKey
from .schema import (
    ProjectIn,
    ProjectKeyIn,
    ProjectKeySchema,
    ProjectOrganizationSchema,
    ProjectSchema,
)

router = Router()


"""
GET /api/0/projects/
GET /api/0/projects/{organization_slug}/{project_slug}/
DELETE /api/0/projects/{organization_slug}/{project_slug}/
POST /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
DELETE /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
GET /api/0/projects/{organization_slug}/{team_slug}/keys/
POST /api/0/projects/{organization_slug}/{team_slug}/keys/
GET /api/0/projects/{organization_slug}/{project_slug}/keys/{key_id}/
DELETE /api/0/projects/{organization_slug}/{project_slug}/keys/{key_id}/
GET /api/0/teams/{organization_slug}/{team_slug}/projects/
POST /api/0/teams/{organization_slug}/{team_slug}/projects/
GET /api/0/organizations/{organization_slug}/projects/
"""


def get_projects_queryset(
    user_id: int, organization_slug: str = None, team_slug: str = None
):
    qs = Project.annotate_is_member(
        Project.undeleted_objects.filter(organization__users=user_id), user_id
    )
    if organization_slug:
        qs = qs.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(teams__slug=team_slug)
    return qs


def get_project_keys_queryset(
    user_id: int,
    organization_slug: str,
    project_slug: str,
    key_id: Optional[UUID] = None,
):
    qs = ProjectKey.objects.filter(
        project__organization__users=user_id,
        project__organization__slug=organization_slug,
        project__slug=project_slug,
    )
    if key_id:
        qs = qs.filter(public_key=key_id)
    return qs


@router.get(
    "projects/",
    response=list[ProjectOrganizationSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read"])
async def list_projects(request: AuthHttpRequest, response: HttpResponse):
    """List all projects that a user has access to"""
    return (
        get_projects_queryset(request.auth.user_id)
        .select_related("organization")
        .order_by("name")
    )


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/",
    response=ProjectOrganizationSchema,
    by_alias=True,
)
@has_permission(["project:read", "project:write", "project:admin"])
async def get_project(
    request: AuthHttpRequest, organization_slug: str, project_slug: str
):
    return await aget_object_or_404(
        get_projects_queryset(request.auth.user_id, organization_slug).select_related(
            "organization"
        ),
        slug=project_slug,
    )


@router.put(
    "projects/{slug:organization_slug}/{slug:project_slug}/",
    response=ProjectOrganizationSchema,
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def update_project(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: ProjectIn,
):
    project = await aget_object_or_404(
        get_projects_queryset(request.auth.user_id, organization_slug).select_related(
            "organization"
        ),
        slug=project_slug,
    )
    for attr, value in payload.dict().items():
        setattr(project, attr, value)
    await project.asave()
    return project


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/",
    response={204: None},
)
@has_permission(["project:admin"])
async def delete_project(
    request: AuthHttpRequest, organization_slug: str, project_slug: str
):
    result, _ = (
        await get_projects_queryset(request.auth.user_id, organization_slug)
        .filter(
            slug=project_slug,
            organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/projects/",
    response=list[ProjectSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read"])
async def list_team_projects(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    team_slug: str,
):
    """List all projects for a given team"""
    return get_projects_queryset(
        request.auth.user_id, organization_slug=organization_slug, team_slug=team_slug
    ).order_by("name")


@router.post(
    "teams/{slug:organization_slug}/{slug:team_slug}/projects/",
    response={201: ProjectSchema},
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def create_project(
    request: AuthHttpRequest, organization_slug: str, team_slug: str, payload: ProjectIn
):
    """Create a new project given a team and organization"""
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        Team,
        slug=team_slug,
        organization__slug=organization_slug,
        organization__users=user_id,
        organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    project = await Project.objects.acreate(organization=organization, **payload.dict())
    await project.teams.aadd(team)
    project = await get_projects_queryset(user_id).aget(id=project.id)
    return 201, project


@router.get(
    "organizations/{slug:organization_slug}/projects/",
    response=list[ProjectTeamSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read"])
async def list_organization_projects(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    query: Optional[str] = None,
):
    """
    Fetch list of organizations for a project
    Contains team information
    query: Filter on team, ex: ?query=!team:burke-software
    """
    queryset = (
        get_projects_queryset(request.auth.user_id, organization_slug=organization_slug)
        .prefetch_related("teams")
        .order_by("name")
    )
    # This query param isn't documented in sentry api but exists
    if query:
        query_parts = query.split()
        for query in query_parts:
            query_part = query.split(":", 1)
            if len(query_part) == 2:
                query_name, query_value = query_part
                if query_name == "team":
                    queryset = queryset.filter(teams__slug=query_value)
                if query_name == "!team":
                    queryset = queryset.exclude(teams__slug=query_value)
    return queryset


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/keys/",
    response=list[ProjectKeySchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read", "project:write", "project:admin"])
async def list_project_keys(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
    status: Optional[str] = None,
):
    """List all DSN keys for a given project"""
    return get_project_keys_queryset(
        request.auth.user_id, organization_slug, project_slug
    )


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response=ProjectKeySchema,
    by_alias=True,
)
@has_permission(["project:read", "project:write", "project:admin"])
async def get_project_key(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    key_id: UUID,
):
    return await aget_object_or_404(
        get_project_keys_queryset(
            request.auth.user_id, organization_slug, project_slug, key_id=key_id
        )
    )


@router.put(
    "projects/{slug:organization_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response=ProjectKeySchema,
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def update_project_key(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    key_id: UUID,
    payload: ProjectKeyIn,
):
    return await aget_object_or_404(
        get_project_keys_queryset(
            request.auth.user_id, organization_slug, project_slug, key_id=key_id
        )
    )


@router.post(
    "projects/{slug:organization_slug}/{slug:project_slug}/keys/",
    response={201: ProjectKeySchema},
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def create_project_key(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    payload: ProjectKeyIn,
):
    """Create new key for project. Rate limiting not implemented."""
    project = await aget_object_or_404(
        get_projects_queryset(request.auth.user_id, organization_slug),
        slug=project_slug,
    )
    return 201, await ProjectKey.objects.acreate(
        project=project,
        name=payload.name,
        rate_limit_count=payload.rate_limit.count if payload.rate_limit else None,
        rate_limit_window=payload.rate_limit.window if payload.rate_limit else None,
    )


@router.delete(
    "projects/{slug:organization_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response={204: None},
)
@has_permission(["project:admin"])
async def delete_project_key(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, key_id: UUID
):
    result, _ = (
        await get_project_keys_queryset(
            request.auth.user_id, organization_slug, project_slug, key_id=key_id
        )
        .filter(
            project__organization__organization_users__role__gte=OrganizationUserRole.ADMIN
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None

from typing import Optional

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from apps.organizations_ext.models import Organization, OrganizationUserRole
from apps.teams.models import Team
from apps.teams.schema import ProjectTeamSchema
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import AuthHttpRequest, has_permission

from .models import Project
from .schema import ProjectIn, ProjectOrganizationSchema, ProjectSchema

router = Router()


"""
GET /api/0/projects/
GET /api/0/teams/{organization_slug}/{team_slug}/projects/
POST /api/0/teams/{organization_slug}/{team_slug}/projects/
POST /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
DELETE /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/ (See teams)
GET /api/0/organizations/{organization_slug}/projects/
"""


def get_projects_queryset(
    user_id: int, organization_slug: str = None, team_slug: str = None
):
    qs = Project.objects.filter(organization__users=user_id).annotate(
        is_member=Count("teams__members", filter=Q(teams__members__id=user_id))
    )
    if organization_slug:
        qs = qs.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(teams__slug=team_slug)
    return qs


@router.get(
    "projects/",
    response=list[ProjectOrganizationSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read"])
async def list_projects(request: AuthHttpRequest, response: HttpResponse):
    return (
        get_projects_queryset(request.auth.user_id)
        .select_related("organization")
        .order_by("name")
    )


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

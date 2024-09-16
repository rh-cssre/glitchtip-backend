from typing import Optional

from django.db.models import Count, Exists, OuterRef, Prefetch
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import paginate

from apps.organizations_ext.models import (
    Organization,
    OrganizationUser,
    OrganizationUserRole,
)
from apps.projects.models import Project
from apps.shared.types import MeID
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .models import Team
from .schema import ProjectTeamSchema, TeamIn, TeamProjectSchema, TeamSchema

router = Router()


"""
OSS Sentry supported
GET /teams/{org}/{team}/
PUT /teams/{org}/{team}/
DELETE /teams/{org}/{team}/
GET /teams/{org}/{team}/members/ (See organizations)
GET /teams/{org}/{team}/projects/ (See projects)
GET /teams/{org}/{team}/stats/ (Not implemented)
GET /organizations/{org}/teams/
POST /organizations/{org}/teams/
POST /organizations/{org}/members/{me|member_id}/teams/{team}/ (join)
DELETE /organizations/{org}/members/{me|member_id}/teams/{team}/ (leave)
GET /api/0/projects/{organization_slug}/{project_slug}/teams/ (Not documented)
POST /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/
DELETE /api/0/projects/{organization_slug}/{project_slug}/teams/{team_slug}/
"""


def get_team_queryset(
    organization_slug: str,
    team_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
    user_id: Optional[int] = None,
    id: Optional[int] = None,
    add_details=False,
    add_projects=False,
):
    qs = Team.objects.filter(organization__slug=organization_slug)
    if team_slug:
        qs = qs.filter(slug=team_slug)
    if project_slug:
        qs = qs.filter(projects__slug=project_slug)
    if id:
        qs = qs.filter(id=id)
    if user_id:
        qs = qs.filter(organization__users=user_id)
        if add_details:
            qs = qs.annotate(
                is_member=Exists(
                    OrganizationUser.objects.filter(
                        teams=OuterRef("pk"), user_id=user_id
                    )
                ),
                member_count=Count("members"),
            )
        if add_projects:
            qs = qs.prefetch_related(
                Prefetch(
                    "projects",
                    queryset=Project.objects.annotate(
                        is_member=Exists(
                            OrganizationUser.objects.filter(
                                teams__members=OuterRef("pk"), user_id=user_id
                            )
                        ),
                    ),
                )
            )
    return qs


@router.get(
    "teams/{slug:organization_slug}/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@has_permission(["team:read", "team:write", "team:admin"])
async def get_team(request: AuthHttpRequest, organization_slug: str, team_slug: str):
    user_id = request.auth.user_id
    return await aget_object_or_404(
        get_team_queryset(
            organization_slug,
            user_id=user_id,
            team_slug=team_slug,
            add_details=True,
            add_projects=True,
        )
    )


@router.put(
    "teams/{slug:organization_slug}/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
async def update_team(
    request: AuthHttpRequest, organization_slug: str, team_slug: str, payload: TeamIn
):
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug,
            user_id=user_id,
            team_slug=team_slug,
            add_details=True,
            add_projects=True,
        )
    )
    team.slug = payload.slug
    await team.asave()
    return team


@router.delete("teams/{slug:organization_slug}/{slug:team_slug}/", response={204: None})
@has_permission(["team:admin"])
async def delete_team(request: AuthHttpRequest, organization_slug: str, team_slug: str):
    result, _ = (
        await get_team_queryset(
            organization_slug, team_slug=team_slug, user_id=request.auth.user_id
        )
        .filter(
            organization__organization_users__role__gte=OrganizationUserRole.ADMIN,
        )
        .adelete()
    )
    if not result:
        raise Http404
    return 204, None


@router.get(
    "/organizations/{slug:organization_slug}/teams/",
    response=list[TeamProjectSchema],
    by_alias=True,
)
@paginate
@has_permission(
    ["team:read", "team:write", "team:admin", "org:read", "org:write", "org:admin"]
)
async def list_teams(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_team_queryset(
        organization_slug,
        user_id=request.auth.user_id,
        add_details=True,
        add_projects=True,
    )


@router.post(
    "/organizations/{slug:organization_slug}/teams/",
    response={201: TeamProjectSchema},
    by_alias=True,
)
@has_permission(["team:write", "team:admin", "org:admin", "org:write"])
async def create_team(
    request: AuthHttpRequest, organization_slug: str, payload: TeamIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=user_id,
        organization_users__role__gte=OrganizationUserRole.ADMIN,
    )
    team = await Team.objects.acreate(organization=organization, slug=payload.slug)
    org_user = await organization.organization_users.filter(user=user_id).afirst()
    await team.members.aadd(org_user)
    return await get_team_queryset(
        organization_slug,
        user_id=user_id,
        id=team.id,
        add_details=True,
        add_projects=True,
    ).aget()


async def modify_member_for_team(
    organization_slug: str,
    member_id: MeID,
    team_slug: str,
    user_id: int,
    add_member=True,
):
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug,
            user_id=user_id,
            team_slug=team_slug,
            add_details=True,
            add_projects=True,
        )
    )
    org_user_qs = OrganizationUser.objects.filter(
        organization__slug=organization_slug
    ).select_related("organization")
    if member_id == "me":
        org_user = await org_user_qs.aget(user_id=user_id)
    else:
        org_user = await aget_object_or_404(org_user_qs, id=member_id)

    open_membership = org_user.organization.open_membership
    is_self = org_user.user_id == user_id

    if not (open_membership and is_self):
        in_team = await team.members.filter(user_id=user_id).aexists()
        if in_team:
            required_role = OrganizationUserRole.ADMIN
        else:
            required_role = OrganizationUserRole.MANAGER

        if not await OrganizationUser.objects.filter(
            user_id=user_id, organization=org_user.organization, role__gte=required_role
        ).aexists():
            raise HttpError(403, "Must be admin to modify teams")

    if add_member:
        await team.members.aadd(org_user)
        team.is_member = True
    else:
        await team.members.aremove(org_user)
    return team


@router.post(
    "/organizations/{slug:organization_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response={201: TeamProjectSchema},
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
async def add_member_to_team(
    request: AuthHttpRequest, organization_slug: str, member_id: MeID, team_slug: str
):
    return 201, await modify_member_for_team(
        organization_slug, member_id, team_slug, request.auth.user_id, True
    )


@router.delete(
    "/organizations/{slug:organization_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
async def delete_member_from_team(
    request: AuthHttpRequest, organization_slug: str, member_id: MeID, team_slug: str
):
    return await modify_member_for_team(
        organization_slug, member_id, team_slug, request.auth.user_id, False
    )


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/teams/",
    response=list[TeamSchema],
    by_alias=True,
)
@paginate
@has_permission(
    ["team:read", "team:write", "team:admin", "org:read", "org:write", "org:admin"]
)
async def list_project_teams(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_team_queryset(
        organization_slug,
        user_id=request.auth.user_id,
        project_slug=project_slug,
        add_details=True,
    )


@router.post(
    "/projects/{slug:organization_slug}/{slug:project_slug}/teams/{slug:team_slug}/",
    response={201: ProjectTeamSchema},
    by_alias=True,
)
@has_permission(["project.write", "project:admin"])
async def add_team_to_project(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    """Add team to project"""
    user_id = request.auth.user_id
    project = await aget_object_or_404(
        Project,
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=user_id,
        organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    )
    team = await aget_object_or_404(
        get_team_queryset(organization_slug, team_slug=team_slug)
    )
    await project.teams.aadd(team)
    project = await (
        Project.annotate_is_member(Project.objects, user_id)
        .prefetch_related("teams")
        .aget(id=project.id)
    )
    return 201, project


@router.delete(
    "/projects/{slug:organization_slug}/{slug:project_slug}/teams/{slug:team_slug}/",
    response=ProjectTeamSchema,
    by_alias=True,
)
@has_permission(["project.write", "project:admin"])
async def delete_team_from_project(
    request: AuthHttpRequest, organization_slug: str, project_slug: str, team_slug: str
):
    """Remove team from project"""
    user_id = request.auth.user_id
    team = await aget_object_or_404(
        get_team_queryset(
            organization_slug, project_slug=project_slug, team_slug=team_slug
        )
    )
    project = await aget_object_or_404(
        Project,
        slug=project_slug,
        organization__slug=organization_slug,
        organization__users=user_id,
        organization__organization_users__role__gte=OrganizationUserRole.MANAGER,
    )
    await project.teams.aremove(team)
    return await (
        Project.annotate_is_member(Project.objects, user_id)
        .prefetch_related("teams")
        .aget(id=project.id)
    )

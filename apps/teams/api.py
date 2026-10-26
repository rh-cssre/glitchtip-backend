from django.db.models import (
    Count,
    Exists,
    F,
    OuterRef,
    Prefetch,
    QuerySet,
    Subquery,
)
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import HttpError

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import OrganizationUser
from apps.organizations_ext.queryset_utils import annotate_actor_role
from apps.projects.models import Project
from apps.shared.types import MeID
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    has_permission,
    org_scoped,
    org_scoped_list,
)

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


def annotate_team_details(qs: QuerySet[Team], user_id: int, add_projects=True):
    qs = qs.annotate(
        is_member=Exists(
            OrganizationUser.objects.filter(teams=OuterRef("pk"), user_id=user_id)
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
    "teams/{slug:organization_id_or_slug}/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@org_scoped(["team:read", "team:write", "team:admin"], Team)
async def get_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    team_slug: str,
):
    return await aget_object_or_404(
        annotate_team_details(qs, request.auth.user_id), slug=team_slug
    )


@router.put(
    "teams/{slug:organization_id_or_slug}/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@org_scoped(["team:write", "team:admin"], Team)
async def update_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    team_slug: str,
    payload: TeamIn,
):
    team = await aget_object_or_404(
        annotate_team_details(qs, request.auth.user_id), slug=team_slug
    )
    team.slug = payload.slug
    await team.asave()
    return team


@router.delete(
    "teams/{slug:organization_id_or_slug}/{slug:team_slug}/", response={204: None}
)
@org_scoped(["team:admin"], Team)
async def delete_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    team_slug: str,
):
    result, _ = await qs.filter(slug=team_slug).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/teams/",
    response=list[TeamProjectSchema],
    by_alias=True,
)
@org_scoped_list(
    ["team:read", "team:write", "team:admin", "org:read", "org:write", "org:admin"],
    Team,
)
async def list_teams(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
):
    return annotate_team_details(Team.objects.all(), request.auth.user_id)


@router.post(
    "/organizations/{slug:organization_id_or_slug}/teams/",
    response={201: TeamProjectSchema},
    by_alias=True,
)
@org_scoped(["team:write", "team:admin", "org:admin", "org:write"], OrganizationUser)
async def create_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[OrganizationUser],
    organization_id_or_slug: str,
    payload: TeamIn,
):
    # The creator joins the team, so their membership row is the read the gate
    # rides on.
    user_id = request.auth.user_id
    org_user = await aget_object_or_404(qs, user_id=user_id)
    team = await Team.objects.acreate(
        organization_id=org_user.organization_id, slug=payload.slug
    )
    await team.members.aadd(org_user)
    return await annotate_team_details(Team.objects.all(), user_id).aget(id=team.id)


async def modify_member_for_team(
    qs: QuerySet[Team],
    member_id: MeID,
    team_slug: str,
    user_id: int,
    add_member=True,
):
    # One query: the team, the member to add or remove, and everything the
    # role rule below needs. annotate_team_details' is_member is whether the
    # caller is in the team.
    target = OrganizationUser.objects.filter(
        organization_id=OuterRef("organization_id")
    )
    target = (
        target.filter(user_id=user_id)
        if member_id == "me"
        else target.filter(id=member_id)
    )
    team = await aget_object_or_404(
        annotate_actor_role(
            annotate_team_details(qs, user_id), user_id, "organization_id"
        ).annotate(
            target_id=Subquery(target.values("id")[:1]),
            target_user_id=Subquery(target.values("user_id")[:1]),
            open_membership=F("organization__open_membership"),
        ),
        slug=team_slug,
    )
    if team.target_id is None:
        raise Http404

    is_self = team.target_user_id == user_id
    if not (team.open_membership and is_self):
        required_role = (
            OrganizationUserRole.ADMIN
            if team.is_member
            else OrganizationUserRole.MANAGER
        )
        if team.actor_role < required_role:
            raise HttpError(403, "Must be admin to modify teams")

    if add_member:
        await team.members.aadd(team.target_id)
        team.is_member = True
    else:
        await team.members.aremove(team.target_id)
    return team


# Tokens need team:write or team:admin; the gate admits every role, because
# any member may join or leave a team under open membership. The role rule is
# checked in modify_member_for_team.
@router.post(
    "/organizations/{slug:organization_id_or_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response={201: TeamProjectSchema},
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
@org_scoped(["team:read", "team:write", "team:admin"], Team)
async def add_member_to_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    member_id: MeID,
    team_slug: str,
):
    return Status(
        201,
        await modify_member_for_team(
            qs, member_id, team_slug, request.auth.user_id, True
        ),
    )


@router.delete(
    "/organizations/{slug:organization_id_or_slug}/members/{slug:member_id}/teams/{slug:team_slug}/",
    response=TeamProjectSchema,
    by_alias=True,
)
@has_permission(["team:write", "team:admin"])
@org_scoped(["team:read", "team:write", "team:admin"], Team)
async def delete_member_from_team(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    member_id: MeID,
    team_slug: str,
):
    return await modify_member_for_team(
        qs, member_id, team_slug, request.auth.user_id, False
    )


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/teams/",
    response=list[TeamSchema],
    by_alias=True,
)
@org_scoped_list(
    ["team:read", "team:write", "team:admin", "org:read", "org:write", "org:admin"],
    Team,
)
async def list_project_teams(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
):
    return annotate_team_details(
        Team.objects.filter(projects__slug=project_slug),
        request.auth.user_id,
        add_projects=False,
    )


async def get_project_teams(project_id: int, user_id: int) -> Project:
    return await (
        Project.annotate_is_member(Project.objects, user_id)
        .prefetch_related("teams")
        .aget(id=project_id)
    )


@router.post(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/teams/{slug:team_slug}/",
    response={201: ProjectTeamSchema},
    by_alias=True,
)
@org_scoped(["org:write"], Project)
async def add_team_to_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    team_slug: str,
):
    """Add team to project"""
    project = await aget_object_or_404(
        qs.annotate(
            team_id=Subquery(
                Team.objects.filter(
                    organization_id=OuterRef("organization_id"), slug=team_slug
                ).values("id")
            )
        ),
        slug=project_slug,
    )
    if project.team_id is None:
        raise Http404
    await project.teams.aadd(project.team_id)
    return Status(201, await get_project_teams(project.id, request.auth.user_id))


@router.delete(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/teams/{slug:team_slug}/",
    response=ProjectTeamSchema,
    by_alias=True,
)
@org_scoped(["org:write"], Project)
async def delete_team_from_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    team_slug: str,
):
    """Remove team from project"""
    project = await aget_object_or_404(
        qs.annotate(
            team_id=Subquery(
                Team.objects.filter(projects=OuterRef("pk"), slug=team_slug).values(
                    "id"
                )
            )
        ),
        slug=project_slug,
    )
    if project.team_id is None:
        raise Http404
    await project.teams.aremove(project.team_id)
    return await get_project_teams(project.id, request.auth.user_id)

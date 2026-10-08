from uuid import UUID

from django.db.models import QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router, Status
from ninja.errors import ValidationError
from ninja.pagination import paginate

from apps.shared.types import MeID
from apps.teams.models import Team
from apps.teams.schema import ProjectTeamSchema
from glitchtip.api.permissions import (
    AuthHttpRequest,
    OrgScopedHttpRequest,
    has_permission,
    org_scoped,
    org_scoped_list,
)

from .models import Project, ProjectKey, UserProjectAlert
from .schema import (
    ProjectIn,
    ProjectKeyIn,
    ProjectKeySchema,
    ProjectOrganizationSchema,
    ProjectSchema,
    StrKeyIntValue,
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
    user_id: int, organization_slug: str | None = None, team_slug: str | None = None
):
    qs = Project.annotate_is_member(
        Project.undeleted_objects.filter(organization__users=user_id), user_id
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
    """List all projects that a user has access to"""
    return (
        get_projects_queryset(request.auth.user_id)
        .select_related("organization")
        .order_by("name")
    )


@router.get(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/",
    response=ProjectOrganizationSchema,
    by_alias=True,
)
@org_scoped(["project:read", "project:write", "project:admin"], Project)
async def get_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
):
    return await aget_object_or_404(
        Project.annotate_is_member(qs, request.auth.user_id).select_related(
            "organization"
        ),
        slug=project_slug,
    )


@router.put(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/",
    response=ProjectOrganizationSchema,
    by_alias=True,
)
@org_scoped(["project:write", "project:admin"], Project)
async def update_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    payload: ProjectIn,
):
    project = await aget_object_or_404(
        Project.annotate_is_member(qs, request.auth.user_id).select_related(
            "organization"
        ),
        slug=project_slug,
    )
    for attr, value in payload.dict(exclude_unset=True).items():
        setattr(project, attr, value)
    await project.asave()
    return project


@router.delete(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/",
    response={204: None},
)
@org_scoped(["project:admin"], Project)
async def delete_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
):
    # Fetched, not qs.adelete(): Project.delete() soft-deletes and enqueues
    # the cleanup task, which a queryset delete would skip.
    project = await aget_object_or_404(qs, slug=project_slug)
    await project.adelete()
    return Status(204, None)


@router.get(
    "teams/{slug:organization_id_or_slug}/{slug:team_slug}/projects/",
    response=list[ProjectSchema],
    by_alias=True,
)
@org_scoped_list(["project:read"], Project)
async def list_team_projects(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    team_slug: str,
):
    """List all projects for a given team"""
    # Filter after annotating: a teams filter first would confine
    # is_member's count to the path's team.
    return (
        Project.annotate_is_member(Project.objects.all(), request.auth.user_id)
        .filter(teams__slug=team_slug)
        .order_by("name")
    )


@router.post(
    "teams/{slug:organization_id_or_slug}/{slug:team_slug}/projects/",
    response={201: ProjectSchema},
    by_alias=True,
)
@org_scoped(["project:write", "project:admin"], Team)
async def create_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Team],
    organization_id_or_slug: str,
    team_slug: str,
    payload: ProjectIn,
):
    """Create a new project given a team and organization"""
    # The slug field's uniqueness check reads project.organization on save.
    team = await aget_object_or_404(qs.select_related("organization"), slug=team_slug)
    project = await Project.objects.acreate(
        organization=team.organization, **payload.dict(exclude_unset=True)
    )
    await project.teams.aadd(team)
    project = await Project.annotate_is_member(
        Project.objects, request.auth.user_id
    ).aget(id=project.id)
    return Status(201, project)


@router.get(
    "organizations/{slug:organization_id_or_slug}/projects/",
    response=list[ProjectTeamSchema],
    by_alias=True,
)
@org_scoped_list(["project:read"], Project)
async def list_organization_projects(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    query: str | None = None,
):
    """
    Fetch list of organizations for a project
    Contains team information
    query: Filter on team, ex: ?query=!team:burke-software
    """
    queryset = (
        Project.annotate_is_member(Project.objects.all(), request.auth.user_id)
        .prefetch_related("teams")
        .order_by("name")
    )
    # This query param isn't documented in sentry api but exists
    if query:
        query_parts = query.split()
        for part in query_parts:
            query_part = part.split(":", 1)
            if len(query_part) == 2:
                query_name, query_value = query_part
                if query_name == "team":
                    queryset = queryset.filter(teams__slug=query_value)
                if query_name == "!team":
                    queryset = queryset.exclude(teams__slug=query_value)
    return queryset


@router.get(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/keys/",
    response=list[ProjectKeySchema],
    by_alias=True,
)
@org_scoped_list(["project:read", "project:write", "project:admin"], ProjectKey)
async def list_project_keys(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
    status: str | None = None,
):
    """List all DSN keys for a given project"""
    return ProjectKey.objects.filter(project__slug=project_slug)


@router.get(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response=ProjectKeySchema,
    by_alias=True,
)
@org_scoped(["project:read", "project:write", "project:admin"], ProjectKey)
async def get_project_key(
    request: OrgScopedHttpRequest,
    qs: QuerySet[ProjectKey],
    organization_id_or_slug: str,
    project_slug: str,
    key_id: UUID,
):
    return await aget_object_or_404(qs, project__slug=project_slug, public_key=key_id)


@router.put(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response=ProjectKeySchema,
    by_alias=True,
)
@org_scoped(["project:write", "project:admin"], ProjectKey)
async def update_project_key(
    request: OrgScopedHttpRequest,
    qs: QuerySet[ProjectKey],
    organization_id_or_slug: str,
    project_slug: str,
    key_id: UUID,
    payload: ProjectKeyIn,
):
    return await aget_object_or_404(qs, project__slug=project_slug, public_key=key_id)


@router.post(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/keys/",
    response={201: ProjectKeySchema},
    by_alias=True,
)
@org_scoped(["project:write", "project:admin"], Project)
async def create_project_key(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Project],
    organization_id_or_slug: str,
    project_slug: str,
    payload: ProjectKeyIn,
):
    """Create new key for project. Rate limiting not implemented."""
    project = await aget_object_or_404(qs, slug=project_slug)
    return Status(
        201,
        await ProjectKey.objects.acreate(
            project=project,
            name=payload.name,
            rate_limit_count=payload.rate_limit.count if payload.rate_limit else None,
            rate_limit_window=payload.rate_limit.window if payload.rate_limit else None,
        ),
    )


@router.delete(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/keys/{uuid:key_id}/",
    response={204: None},
)
@org_scoped(["project:admin"], ProjectKey)
async def delete_project_key(
    request: OrgScopedHttpRequest,
    qs: QuerySet[ProjectKey],
    organization_id_or_slug: str,
    project_slug: str,
    key_id: UUID,
):
    result, _ = await qs.filter(project__slug=project_slug, public_key=key_id).adelete()
    if not result:
        raise Http404
    return Status(204, None)


@router.get("/users/{slug:user_id}/notifications/alerts/", response=StrKeyIntValue)
async def user_notification_alerts(request: AuthHttpRequest, user_id: MeID):
    """
    Returns dictionary of project_id: status. Now project_id status means it's "default"

    To update, submit `{project_id: status}` where status is -1 (default), 0, or 1
    """
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id

    data = {}
    async for alert in UserProjectAlert.objects.filter(user_id=user_id):
        data[str(alert.project_id)] = alert.status
    return data


@router.put("/users/{slug:user_id}/notifications/alerts/", response={204: None})
async def update_user_notification_alerts(
    request: AuthHttpRequest, user_id: MeID, payload: StrKeyIntValue
):
    if user_id != request.auth.user_id and user_id != "me":
        raise Http404
    user_id = request.auth.user_id
    items = [x for x in payload.root.items()]
    if len(items) != 1:
        raise ValidationError("Invalid alert format, expected one value")

    project_id, alert_status = items[0]
    if alert_status not in [1, 0, -1]:
        raise ValidationError("Invalid status, must be -1, 0, or 1")

    alert = await UserProjectAlert.objects.filter(
        user_id=user_id, project_id=project_id
    ).afirst()
    if alert_status == -1:
        if alert:
            await alert.adelete()
    else:
        await UserProjectAlert.objects.aupdate_or_create(
            user_id=user_id, project_id=project_id, defaults={"status": alert_status}
        )

    return Status(204, None)

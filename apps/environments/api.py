from typing import Literal

from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from .models import Environment, EnvironmentProject
from .schema import EnvironmentProjectIn, EnvironmentProjectSchema, EnvironmentSchema

router = Router()


"""
GET /api/0/organizations/{organization_slug}/environments/
GET /api/0/projects/{organization_slug}/{project_slug}/environments/ (Not documented)
PUT /api/0/projects/{organization_slug}/{project_slug}/environments/{slug}/ (Not documented)
"""


Visibility = Literal["all", "hidden", "visible"]


@router.get(
    "organizations/{slug:organization_slug}/environments/",
    response=list[EnvironmentSchema],
)
@paginate
@has_permission(["org:read", "org:write", "org:admin"])
async def list_environments(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    visibility: Visibility = "visible",
):
    qs = Environment.objects.filter(
        organization__users=request.auth.user_id,
        organization__slug=organization_slug,
    )
    if visibility == "hidden":
        qs = qs.filter(environmentproject__is_hidden=True)
    if visibility == "visible":
        qs = qs.filter(environmentproject__is_hidden=False)
    return qs


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/environments/",
    response=list[EnvironmentProjectSchema],
    by_alias=True,
)
@paginate
@has_permission(["project:read", "project:write", "project:admin"])
async def list_environment_projects(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
    visibility: Visibility = "visible",
):
    qs = EnvironmentProject.objects.filter(
        environment__organization__users=request.auth.user_id,
        environment__organization__slug=organization_slug,
        project__slug=project_slug,
    )
    if visibility == "hidden":
        qs = qs.filter(is_hidden=True)
    if visibility == "visible":
        qs = qs.filter(is_hidden=False)
    return qs.select_related("environment")


@router.put(
    "projects/{slug:organization_slug}/{slug:project_slug}/environments/{str:name}/",
    response=EnvironmentProjectSchema,
    by_alias=True,
)
@has_permission(["project:write", "project:admin"])
async def update_environment_project(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    name: str,
    payload: EnvironmentProjectIn,
):
    environment = await aget_object_or_404(
        EnvironmentProject,
        environment__organization__users=request.auth.user_id,
        environment__organization__slug=organization_slug,
        project__slug=project_slug,
        environment__name=name,
    )
    for attr, value in payload.dict().items():
        setattr(environment, attr, value)
    await environment.asave()
    return await EnvironmentProject.objects.select_related("environment").aget(
        id=environment.id
    )

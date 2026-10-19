from typing import Literal

from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router

from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    org_scoped,
    org_scoped_list,
)

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
    "organizations/{slug:organization_id_or_slug}/environments/",
    response=list[EnvironmentSchema],
)
@org_scoped_list(["org:read", "org:write", "org:admin"], Environment)
async def list_environments(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    visibility: Visibility = "visible",
):
    qs = Environment.objects.all()
    if visibility == "hidden":
        qs = qs.filter(environmentproject__is_hidden=True)
    if visibility == "visible":
        qs = qs.filter(environmentproject__is_hidden=False)
    return qs


@router.get(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/environments/",
    response=list[EnvironmentProjectSchema],
    by_alias=True,
)
@org_scoped_list(["project:read", "project:write", "project:admin"], EnvironmentProject)
async def list_environment_projects(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
    visibility: Visibility = "visible",
):
    qs = EnvironmentProject.objects.filter(project__slug=project_slug)
    if visibility == "hidden":
        qs = qs.filter(is_hidden=True)
    if visibility == "visible":
        qs = qs.filter(is_hidden=False)
    return qs.select_related("environment")


@router.put(
    "projects/{slug:organization_id_or_slug}/{slug:project_slug}/environments/{str:name}/",
    response=EnvironmentProjectSchema,
    by_alias=True,
)
@org_scoped(["project:write", "project:admin"], EnvironmentProject)
async def update_environment_project(
    request: OrgScopedHttpRequest,
    qs: QuerySet[EnvironmentProject],
    organization_id_or_slug: str,
    project_slug: str,
    name: str,
    payload: EnvironmentProjectIn,
):
    environment = await aget_object_or_404(
        qs.select_related("environment"),
        project__slug=project_slug,
        environment__name=name,
    )
    for attr, value in payload.dict().items():
        setattr(environment, attr, value)
    await environment.asave()
    return environment

import uuid
from typing import Optional

from django.http import Http404
from ninja import Router

from glitchtip.api.authentication import AuthHttpRequest
from .schema import IssueEventSchema
from .models import IssueEvent


router = Router()


def get_queryset(
    request: AuthHttpRequest,
    issue_id: Optional[int] = None,
    organization_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
):
    user_id = request.auth
    qs = IssueEvent.objects.filter(issue__project__organization__users=user_id)
    if issue_id:
        qs = qs.filter(issue_id=issue_id)
    if organization_slug:
        qs = qs.filter(issue__project__organization__slug=organization_slug)
    if project_slug:
        qs = qs.filter(issue__project__slug=project_slug)
    return qs.select_related("issue")


@router.get(
    "/issues/{int:issue_id}/events/", response=list[IssueEventSchema], by_alias=True
)
async def issue_event_list(request: AuthHttpRequest, issue_id: int):
    return [obj async for obj in get_queryset(request, issue_id=issue_id)]


@router.get(
    "/issues/{int:issue_id}/events/latest/", response=IssueEventSchema, by_alias=True
)
async def issue_event_latest(request: AuthHttpRequest, issue_id: int):
    obj = await get_queryset(request, issue_id).afirst()
    if not obj:
        raise Http404()
    return obj


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventSchema,
    by_alias=True,
)
async def issue_event_retrieve(
    request: AuthHttpRequest, issue_id: int, event_id: uuid.UUID
):
    qs = get_queryset(request, issue_id)
    try:
        return await qs.aget(id=event_id)
    except IssueEvent.DoesNotExist:
        raise Http404()


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/",
    response=list[IssueEventSchema],
    by_alias=True,
)
async def project_issue_event_list(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
):
    print("dep")
    return [
        obj
        async for obj in get_queryset(
            request, organization_slug=organization_slug, project_slug=project_slug
        )
    ]


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/{event_id}/",
    response=IssueEventSchema,
    by_alias=True,
)
async def project_issue_event_retrieve(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    event_id: uuid.UUID,
):
    qs = get_queryset(
        request, organization_slug=organization_slug, project_slug=project_slug
    )
    try:
        return await qs.aget(id=event_id)
    except IssueEvent.DoesNotExist:
        raise Http404()

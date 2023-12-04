import uuid
from typing import Optional

from django.db.models import OuterRef, Subquery, Window
from django.db.models.functions import Lag
from django.http import Http404

from glitchtip.api.authentication import AuthHttpRequest

from ..models import IssueEvent
from ..schema import IssueEventDetailSchema, IssueEventJsonSchema, IssueEventSchema
from . import router


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
    return qs.select_related("issue").order_by("-received")


@router.get(
    "/issues/{int:issue_id}/events/", response=list[IssueEventSchema], by_alias=True
)
async def list_issue_event(request: AuthHttpRequest, issue_id: int):
    return [obj async for obj in get_queryset(request, issue_id=issue_id)]


@router.get(
    "/issues/{int:issue_id}/events/latest/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
async def get_latest_issue_event(request: AuthHttpRequest, issue_id: int):
    qs = get_queryset(request, issue_id)
    qs = qs.annotate(
        previous=Window(expression=Lag("id"), order_by="received"),
    )
    obj = await qs.afirst()
    if not obj:
        raise Http404()
    obj.next = None  # We know the next after "latest" must be None
    return obj


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
async def get_issue_event(request: AuthHttpRequest, issue_id: int, event_id: uuid.UUID):
    qs = get_queryset(request, issue_id)
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(received__lt=OuterRef("received")).values("id")[:1]
        ),
        next=Subquery(qs.filter(received__gt=OuterRef("received")).values("id")[:1]),
    )
    try:
        return await qs.filter(id=event_id).aget()
    except IssueEvent.DoesNotExist:
        raise Http404()


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/",
    response=list[IssueEventSchema],
    by_alias=True,
)
async def list_project_issue_event(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
):
    return [
        obj
        async for obj in get_queryset(
            request, organization_slug=organization_slug, project_slug=project_slug
        )
    ]


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
async def get_project_issue_event(
    request: AuthHttpRequest,
    organization_slug: str,
    project_slug: str,
    event_id: uuid.UUID,
):
    qs = get_queryset(
        request, organization_slug=organization_slug, project_slug=project_slug
    )
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(received__lt=OuterRef("received")).values("id")[:1]
        ),
        next=Subquery(qs.filter(received__gt=OuterRef("received")).values("id")[:1]),
    )
    try:
        return await qs.aget(id=event_id)
    except IssueEvent.DoesNotExist:
        raise Http404()


@router.get(
    "/organizations/{slug:organization_slug}/issues/{int:issue_id}/events/{event_id}/json/",
    response=IssueEventJsonSchema,
    by_alias=True,
)
async def get_event_json(
    request: AuthHttpRequest, organization_slug: str, issue_id: int, event_id: uuid.UUID
):
    qs = get_queryset(request, organization_slug=organization_slug, issue_id=issue_id)
    try:
        return await qs.aget(id=event_id)
    except IssueEvent.DoesNotExist:
        raise Http404()

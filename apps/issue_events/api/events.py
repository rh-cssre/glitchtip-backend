import uuid

from django.db.models import OuterRef, Subquery
from django.http import Http404, HttpResponse
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from ..models import IssueEvent, UserReport
from ..schema import IssueEventDetailSchema, IssueEventJsonSchema, IssueEventSchema
from . import router


def get_queryset(
    request: AuthHttpRequest,
    issue_id: int | None = None,
    organization_slug: str | None = None,
    project_slug: str | None = None,
):
    user_id = request.auth.user_id
    qs = IssueEvent.objects.filter(issue__project__organization__users=user_id)
    if issue_id:
        qs = qs.filter(issue_id=issue_id)
    if organization_slug:
        qs = qs.filter(issue__project__organization__slug=organization_slug)
    if project_slug:
        qs = qs.filter(issue__project__slug=project_slug)
    return qs.select_related("issue")


async def get_user_report(event_id: uuid.UUID) -> UserReport | None:
    return await UserReport.objects.filter(event_id=event_id).afirst()


@router.get("/issues/{int:issue_id}/events/", response=list[IssueEventSchema])
@paginate
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_event(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    return get_queryset(request, issue_id=issue_id).order_by("-received")


@router.get(
    "/issues/{int:issue_id}/events/latest/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_latest_issue_event(request: AuthHttpRequest, issue_id: int):
    qs = get_queryset(request, issue_id).order_by("-received")
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(received__lt=OuterRef("received"))
            .order_by("-received")
            .values("id")[:1]
        ),
    )
    event = await qs.afirst()
    if not event:
        raise Http404()
    event.next = None  # We know the next after "latest" must be None
    event.user_report = await get_user_report(event.id)
    return event


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_issue_event(request: AuthHttpRequest, issue_id: int, event_id: uuid.UUID):
    qs = get_queryset(request, issue_id)
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(received__lt=OuterRef("received"))
            .order_by("-received")
            .values("id")[:1]
        ),
        next=Subquery(
            qs.filter(received__gt=OuterRef("received"))
            .order_by("received")
            .values("id")[:1]
        ),
    )
    event = await qs.filter(id=event_id).afirst()
    if not event:
        raise Http404()
    event.user_report = await get_user_report(event.id)
    return event


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/",
    response=list[IssueEventSchema],
    by_alias=True,
)
@paginate
@has_permission(["event:read", "event:write", "event:admin"])
async def list_project_issue_event(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
):
    return get_queryset(
        request, organization_slug=organization_slug, project_slug=project_slug
    ).order_by("-received")


@router.get(
    "/projects/{slug:organization_slug}/{slug:project_slug}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
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
            qs.filter(received__lt=OuterRef("received"))
            .order_by("-received")
            .values("id")[:1]
        ),
        next=Subquery(
            qs.filter(received__gt=OuterRef("received"))
            .order_by("received")
            .values("id")[:1]
        ),
    )
    event = await qs.filter(id=event_id).afirst()
    if not event:
        raise Http404()
    event.user_report = await get_user_report(event.id)
    return event


@router.get(
    "/organizations/{slug:organization_slug}/issues/{int:issue_id}/events/{event_id}/json/",
    response=IssueEventJsonSchema,
    by_alias=True,
    exclude_none=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_event_json(
    request: AuthHttpRequest, organization_slug: str, issue_id: int, event_id: uuid.UUID
):
    qs = get_queryset(request, organization_slug=organization_slug, issue_id=issue_id)
    obj = await qs.filter(id=event_id).aget()
    if not obj:
        return Http404()
    return obj

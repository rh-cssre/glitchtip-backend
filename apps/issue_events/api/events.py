import asyncio
import uuid
from datetime import datetime, timezone

from django.db.models import OuterRef, Subquery
from django.http import Http404, HttpResponse
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission
from glitchtip.partition_manager import UUID7Helper

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


def _get_event_from_cold(event_id: uuid.UUID, organization_id: int):
    """Try to find an event in cold storage by its UUIDv7 id."""
    from ..cold_storage import get_event_from_cold, is_duckdb_available

    if not is_duckdb_available():
        return None

    try:
        event_time = UUID7Helper.extract_datetime(event_id)
    except ValueError:
        return None
    return get_event_from_cold(organization_id, event_id, event_time)


def _get_cold_events_for_issue(
    issue_id: int,
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    limit: int = 100,
):
    """Query cold storage for events belonging to an issue."""
    from ..cold_storage import is_duckdb_available, query_cold_events

    if not is_duckdb_available():
        return []

    return query_cold_events(
        organization_id=organization_id,
        start_dt=start_dt,
        end_dt=end_dt,
        issue_id=issue_id,
        limit=limit,
    )


@router.get("/issues/{int:issue_id}/events/", response=list[IssueEventSchema])
@paginate
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_event(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    # Order by -id (UUIDv7) for partition pruning; equivalent to -received ordering
    return get_queryset(request, issue_id=issue_id).order_by("-id")


@router.get(
    "/issues/{int:issue_id}/events/latest/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_latest_issue_event(request: AuthHttpRequest, issue_id: int):
    # Order by -id (UUIDv7) for partition pruning; equivalent to -received ordering
    qs = get_queryset(request, issue_id).order_by("-id")
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(id__lt=OuterRef("id")).order_by("-id").values("id")[:1]
        ),
    )
    event = await qs.afirst()
    if event:
        event.next = None  # We know the next after "latest" must be None
        event.user_report = await get_user_report(event.id)
        return event

    # Fall back to cold storage
    from ..cold_storage import is_duckdb_available

    if not is_duckdb_available():
        raise Http404()

    from ..models import Issue

    issue = (
        await Issue.objects.filter(
            id=issue_id, project__organization__users=request.auth.user_id
        )
        .select_related("project__organization")
        .afirst()
    )
    if not issue:
        raise Http404()

    cold_event = await asyncio.to_thread(
        _get_cold_events_for_issue,
        issue_id=issue_id,
        organization_id=issue.project.organization_id,
        start_dt=datetime.min.replace(tzinfo=timezone.utc),
        end_dt=datetime.now(timezone.utc),
        limit=1,
    )
    if not cold_event:
        raise Http404()

    event = cold_event[0]
    event.issue = issue
    event.previous = None
    event.next = None
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
    # Use id (UUIDv7) for prev/next navigation - enables partition pruning
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(id__lt=OuterRef("id")).order_by("-id").values("id")[:1]
        ),
        next=Subquery(qs.filter(id__gt=OuterRef("id")).order_by("id").values("id")[:1]),
    )
    event = await qs.filter(id=event_id).afirst()
    if event:
        event.user_report = await get_user_report(event.id)
        return event

    # Fall back to cold storage
    from ..models import Issue

    issue = (
        await Issue.objects.filter(
            id=issue_id, project__organization__users=request.auth.user_id
        )
        .select_related("project__organization")
        .afirst()
    )
    if not issue:
        raise Http404()

    cold_event = await asyncio.to_thread(
        _get_event_from_cold, event_id, issue.project.organization_id
    )
    if not cold_event:
        raise Http404()

    cold_event.issue = issue
    cold_event.previous = None
    cold_event.next = None
    cold_event.user_report = await get_user_report(cold_event.id)
    return cold_event


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
    # Order by -id (UUIDv7) for partition pruning; equivalent to -received ordering
    return get_queryset(
        request, organization_slug=organization_slug, project_slug=project_slug
    ).order_by("-id")


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
    # Use id (UUIDv7) for prev/next navigation - enables partition pruning
    qs = qs.annotate(
        previous=Subquery(
            qs.filter(id__lt=OuterRef("id")).order_by("-id").values("id")[:1]
        ),
        next=Subquery(qs.filter(id__gt=OuterRef("id")).order_by("id").values("id")[:1]),
    )
    event = await qs.filter(id=event_id).afirst()
    if event:
        event.user_report = await get_user_report(event.id)
        return event

    # Fall back to cold storage
    from apps.organizations_ext.models import Organization

    org = await Organization.objects.filter(
        slug=organization_slug, users=request.auth.user_id
    ).afirst()
    if not org:
        raise Http404()

    cold_event = await asyncio.to_thread(_get_event_from_cold, event_id, org.id)
    if not cold_event:
        raise Http404()

    # Attach issue for schema resolution
    from ..models import Issue

    issue = (
        await Issue.objects.filter(id=cold_event.issue_id)
        .select_related("project")
        .afirst()
    )
    cold_event.issue = issue
    cold_event.previous = None
    cold_event.next = None
    cold_event.user_report = await get_user_report(cold_event.id)
    return cold_event


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
    obj = await qs.filter(id=event_id).afirst()
    if obj:
        return obj

    # Fall back to cold storage
    from ..models import Issue

    issue = (
        await Issue.objects.filter(
            id=issue_id,
            project__organization__slug=organization_slug,
            project__organization__users=request.auth.user_id,
        )
        .select_related("project__organization")
        .afirst()
    )
    if not issue:
        raise Http404()

    cold_event = await asyncio.to_thread(
        _get_event_from_cold, event_id, issue.project.organization_id
    )
    if not cold_event:
        raise Http404()

    cold_event.issue = issue
    return cold_event

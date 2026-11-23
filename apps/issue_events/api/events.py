import asyncio
import uuid
from datetime import datetime, timedelta

from django.db.models import OuterRef, QuerySet, Subquery
from django.http import Http404, HttpResponse
from ninja.pagination import paginate

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import MissOnEmptyPagination
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    has_permission,
    org_scoped,
    org_scoped_list,
)
from glitchtip.cold_storage import ColdStorageBusy, cold_read_slot
from glitchtip.partition_manager import UUID7Helper

from ..models import Issue, IssueEvent, UserReport
from ..schema import IssueEventDetailSchema, IssueEventJsonSchema, IssueEventSchema
from ..services import is_uuid7
from . import router


def get_queryset(request: AuthHttpRequest, issue_id: int | None = None):
    user_id = request.auth.user_id
    qs = IssueEvent.objects.filter(issue__project__organization__users=user_id)
    if issue_id:
        qs = qs.filter(issue_id=issue_id)
    return qs.select_related("issue")


async def get_user_report(event_id: uuid.UUID) -> UserReport | None:
    return await UserReport.objects.filter(event_id=event_id).afirst()


async def _resolve_issue(request: AuthHttpRequest, issue_id: int) -> Issue:
    """Resolve an issue the caller is a member of, or 404.

    Everything downstream scopes by ``issue.project.organization_id``, which
    prunes the IssueEvent hash sub-partitions without joining back to
    organization.
    """
    qs = Issue.objects.filter(
        id=issue_id, project__organization__users=request.auth.user_id
    )
    issue = await qs.select_related("project__organization", "index").afirst()
    if not issue:
        raise Http404()
    return issue


def _get_issue_event_queryset(
    request: AuthHttpRequest, issue_id: int, organization_id: int
):
    """Hot-table queryset for one issue, annotated for prev/next navigation.

    ``organization_id`` is filtered in before annotating so the constant lands on
    the prev/next subqueries too - they scan the same partitioned hot table and
    would otherwise hit every hash sub-partition.
    """
    qs = get_queryset(request, issue_id).filter(organization_id=organization_id)
    # Use id (UUIDv7) for prev/next navigation - enables partition pruning
    return qs.annotate(
        previous=Subquery(
            qs.filter(id__lt=OuterRef("id")).order_by("-id").values("id")[:1]
        ),
        next=Subquery(qs.filter(id__gt=OuterRef("id")).order_by("id").values("id")[:1]),
    )


async def _get_latest_event_for_issue(
    request: AuthHttpRequest,
    issue: Issue,
    issue_id: int,
):
    """Return the latest IssueEvent for an already-resolved issue.

    Shared by the bare and organization-scoped ``.../events/latest/`` routes so the
    hash-partition pruning, the ``previous`` subquery, and the cold-storage fallback
    live in one place. The caller is responsible for resolving ``issue``, which
    already pins the organization - so the hot queryset is scoped by
    ``organization_id`` alone, never by slug.
    """
    # Filter by organization_id to enable hash sub-partition pruning
    qs = (
        get_queryset(request, issue_id)
        .filter(organization_id=issue.project.organization_id)
        .order_by("-id")
    )
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

    # Narrow date range using issue.last_seen to avoid scanning all cold
    # storage files. Buffer by 1 day to account for clock skew.
    cold_end = issue.last_seen + timedelta(days=1)
    cold_start = issue.last_seen - timedelta(days=1)
    cold_event = await asyncio.to_thread(
        _get_cold_events_for_issue,
        issue_id=issue_id,
        organization_id=issue.project.organization_id,
        start_dt=cold_start,
        end_dt=cold_end,
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


async def _get_issue_event(request: AuthHttpRequest, issue: Issue, event_id: uuid.UUID):
    """Return one event for an already-resolved issue: hot table, then cold storage.

    Shared by the bare and organization-scoped ``.../events/{event_id}/`` routes.
    The caller resolves ``issue`` and so pins the organization; the slug never
    reaches this far.
    """
    org_id = issue.project.organization_id
    # organization_id is already filtered in - it prunes the hash sub-partitions for
    # the event lookup and for the prev/next subqueries alike.
    qs = _get_issue_event_queryset(request, issue.id, org_id)

    # A UUIDv7 event_id is our own primary key; anything else is a client-provided
    # sentry SDK event_id (typically UUIDv4).
    if is_uuid7(event_id):
        event = await qs.filter(id=event_id).afirst()
    else:
        event = await qs.filter(event_id=event_id).afirst()

    if event:
        event.user_report = await get_user_report(event.id)
        return event

    # Fall back to cold storage
    # Slot taken on the loop, not inside the thread: waiting here costs a
    # coroutine, waiting in the thread pins a pool thread and grows the
    # executor (and with it the number of cached DuckDB connections).
    async with cold_read_slot() as slot:
        if not slot:
            raise ColdStorageBusy()
        cold_event = await asyncio.to_thread(_get_event_from_cold, event_id, org_id)
    if not cold_event:
        raise Http404()

    cold_event.issue = issue
    cold_event.previous = None
    cold_event.next = None
    cold_event.user_report = await get_user_report(cold_event.id)
    return cold_event


@router.get("/issues/{int:issue_id}/events/", response=list[IssueEventSchema])
@paginate
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_event(
    request: AuthHttpRequest, response: HttpResponse, issue_id: int
):
    # Order by -id (UUIDv7) for partition pruning; equivalent to -received ordering
    return get_queryset(request, issue_id=issue_id).order_by("-id")


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/events/",
    response=list[IssueEventSchema],
)
@org_scoped_list(
    ["event:read", "event:write", "event:admin"],
    IssueEvent,
    pagination=MissOnEmptyPagination,
)
async def list_organization_issue_event(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    issue_id: int,
    organization_id_or_slug: str,
):
    # The gate filters organization_id, which prunes the hash sub-partitions
    # without joining issue -> project -> organization. Order by -id (UUIDv7)
    # for partition pruning; equivalent to -received ordering.
    return (
        IssueEvent.objects.filter(issue_id=issue_id)
        .select_related("issue")
        .order_by("-id")
    )


@router.get(
    "/issues/{int:issue_id}/events/latest/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_latest_issue_event(request: AuthHttpRequest, issue_id: int):
    # Resolve issue first to get organization_id for hash partition pruning
    issue = await _resolve_issue(request, issue_id)
    return await _get_latest_event_for_issue(request, issue, issue_id)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/events/latest/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def get_organization_latest_issue_event(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
):
    issue = await (
        qs.filter(id=issue_id).select_related("project__organization", "index").afirst()
    )
    if not issue:
        raise Http404()
    return await _get_latest_event_for_issue(request, issue, issue_id)


@router.get(
    "/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_issue_event(request: AuthHttpRequest, issue_id: int, event_id: uuid.UUID):
    issue = await _resolve_issue(request, issue_id)
    return await _get_issue_event(request, issue, event_id)


@router.get(
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Issue)
async def get_organization_issue_event(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Issue],
    organization_id_or_slug: str,
    issue_id: int,
    event_id: uuid.UUID,
):
    issue = await (
        qs.filter(id=issue_id).select_related("project__organization", "index").afirst()
    )
    if not issue:
        raise Http404()
    return await _get_issue_event(request, issue, event_id)


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/events/",
    response=list[IssueEventSchema],
    by_alias=True,
)
@org_scoped_list(["event:read", "event:write", "event:admin"], IssueEvent)
async def list_project_issue_event(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    organization_id_or_slug: str,
    project_slug: str,
):
    # The gate filters the event's own organization_id, which leaves the
    # project join unscoped; project slugs repeat across orgs, so without the
    # org id the planner can't start from the one project. Order by -id
    # (UUIDv7) for partition pruning; equivalent to -received ordering.
    return (
        IssueEvent.objects.filter(
            issue__project__organization_id=request.org_scope.org_id,
            issue__project__slug=project_slug,
        )
        .select_related("issue")
        .order_by("-id")
    )


@router.get(
    "/projects/{slug:organization_id_or_slug}/{slug:project_slug}/events/{event_id}/",
    response=IssueEventDetailSchema,
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], IssueEvent)
async def get_project_issue_event(
    request: OrgScopedHttpRequest,
    qs: QuerySet[IssueEvent],
    organization_id_or_slug: str,
    project_slug: str,
    event_id: uuid.UUID,
):
    org_id = request.org_scope.org_id
    # The prev/next subqueries only run for an event the gate already let
    # through, so they need the partition key, not the gate.
    siblings = IssueEvent.objects.filter(
        organization_id=org_id, issue__project__slug=project_slug
    )
    qs = (
        qs.filter(issue__project__slug=project_slug)
        .select_related("issue")
        .annotate(
            previous=Subquery(
                siblings.filter(id__lt=OuterRef("id")).order_by("-id").values("id")[:1]
            ),
            next=Subquery(
                siblings.filter(id__gt=OuterRef("id")).order_by("id").values("id")[:1]
            ),
        )
    )

    # A UUIDv7 event_id is our own primary key; anything else is a
    # client-provided sentry SDK event_id (typically UUIDv4).
    if is_uuid7(event_id):
        event = await qs.filter(id=event_id).afirst()
    else:
        event = await qs.filter(event_id=event_id).afirst()

    if event:
        event.user_report = await get_user_report(event.id)
        return event

    # Fall back to cold storage, which can't carry the gate: prove membership
    # first.
    if error := await request.org_scope.explain_miss():
        raise error

    async with cold_read_slot() as slot:
        if not slot:
            raise ColdStorageBusy()
        cold_event = await asyncio.to_thread(_get_event_from_cold, event_id, org_id)
    if not cold_event:
        raise Http404()

    # Attach issue for schema resolution
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
    "/organizations/{slug:organization_id_or_slug}/issues/{int:issue_id}/events/{event_id}/json/",
    response=IssueEventJsonSchema,
    by_alias=True,
    exclude_none=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], IssueEvent)
async def get_event_json(
    request: OrgScopedHttpRequest,
    qs: QuerySet[IssueEvent],
    organization_id_or_slug: str,
    issue_id: int,
    event_id: uuid.UUID,
):
    qs = qs.filter(issue_id=issue_id).select_related("issue")

    if is_uuid7(event_id):
        obj = await qs.filter(id=event_id).afirst()
    else:
        obj = await qs.filter(event_id=event_id).afirst()

    if obj:
        return obj

    # Fall back to cold storage, which can't carry the gate: it rides on the
    # issue read instead.
    issue = await (
        request.org_scope.apply(Issue.objects.filter(id=issue_id))
        .select_related("project__organization", "index")
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

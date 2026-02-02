from collections import defaultdict
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Router
from ninja.pagination import paginate

from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import AsyncLinkHeaderPagination

from .models import Monitor, MonitorCheck, StatusPage
from .schema import (
    MonitorCheckResponseTimeSchema,
    MonitorCheckSchema,
    MonitorDetailSchema,
    MonitorIn,
    MonitorSchema,
    StatusPageIn,
    StatusPageSchema,
)
from .tasks import send_monitor_notification

router = Router()


class MonitorPagination(AsyncLinkHeaderPagination):
    """Custom pagination that efficiently fetches checks using LATERAL JOIN."""

    async def apaginate_queryset(
        self, queryset, pagination, request, response, **params
    ):
        page = await super().apaginate_queryset(
            queryset, pagination, request, response, **params
        )
        # Fetch checks for just the paginated monitors using efficient LATERAL JOIN
        await attach_checks_to_monitors(page)
        return page


def get_monitor_queryset(user_id: int, organization_slug: str):
    """Get monitors with annotations but WITHOUT checks prefetch."""
    return (
        Monitor.objects.with_check_annotations()
        .filter(organization__users=user_id, organization__slug=organization_slug)
        .select_related("project", "organization")
    )


async def fetch_checks_lateral(
    monitor_ids: list[int], limit: int = 60
) -> dict[int, list[MonitorCheck]]:
    """
    Efficiently fetch top N checks per monitor using LATERAL JOIN.

    This is much faster than window functions because:
    - Uses the (monitor_id, start_check DESC) index efficiently
    - Stops scanning after `limit` rows per monitor (early termination)
    - No need to process all historical checks

    Returns a dict mapping monitor_id -> list of MonitorCheck instances.
    """
    if not monitor_ids:
        return {}

    sql = """
        SELECT c.id, c.organization_id, c.monitor_id, c.start_check,
               c.response_time, c.reason, c.is_up, c.is_change, c.data
        FROM unnest(%(monitor_ids)s::int[]) AS m(id)
        CROSS JOIN LATERAL (
            SELECT id, organization_id, monitor_id, start_check,
                   response_time, reason, is_up, is_change, data
            FROM uptime_monitorcheck
            WHERE monitor_id = m.id
            ORDER BY start_check DESC
            LIMIT %(limit)s
        ) c
        ORDER BY c.monitor_id, c.start_check DESC
    """

    def execute_query():
        with connection.cursor() as cursor:
            cursor.execute(sql, {"monitor_ids": monitor_ids, "limit": limit})
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    rows = await sync_to_async(execute_query)()

    checks_by_monitor: dict[int, list[MonitorCheck]] = defaultdict(list)
    for row in rows:
        check = MonitorCheck(
            id=row["id"],
            organization_id=row["organization_id"],
            monitor_id=row["monitor_id"],
            start_check=row["start_check"],
            response_time=row["response_time"],
            reason=row["reason"],
            is_up=row["is_up"],
            is_change=row["is_change"],
            data=row["data"],
        )
        checks_by_monitor[row["monitor_id"]].append(check)

    return dict(checks_by_monitor)


async def attach_checks_to_monitors(
    monitors: list[Monitor], limit: int = 60
) -> list[Monitor]:
    """Fetch and attach checks to a list of monitors using LATERAL JOIN."""
    if not monitors:
        return monitors
    monitor_ids = [m.id for m in monitors]
    checks_by_monitor = await fetch_checks_lateral(monitor_ids, limit)
    for monitor in monitors:
        # Use Django's prefetch cache so serializers see the checks
        monitor._prefetched_objects_cache = {
            "checks": checks_by_monitor.get(monitor.id, [])
        }
    return monitors


@router.post(
    "organizations/{slug:organization_slug}/heartbeat_check/{uuid:endpoint_id}/",
    response=MonitorCheckSchema,
    auth=None,
)
async def heartbeat_check(
    request: HttpRequest, organization_slug: str, endpoint_id: UUID
):
    """
    Heartbeat monitors allow an external service to contact this endpoint
    when the service is up.
    """
    monitor = await aget_object_or_404(
        Monitor.objects.with_check_annotations().select_related("organization"),
        organization__slug=organization_slug,
        endpoint_id=endpoint_id,
    )
    monitor_check = await MonitorCheck.objects.acreate(
        monitor=monitor,
        organization=monitor.organization,
        is_up=True,
        reason=None,
        is_change=monitor.latest_is_up is not True,
    )
    if monitor.latest_is_up is False:
        last_change = monitor.last_change
        if last_change:
            last_change = last_change.isoformat()
        monitor_check_pk = [str(monitor_check.id), monitor.organization.id]
        await send_monitor_notification.aenqueue(
            monitor.id, monitor_check_pk, False, last_change
        )

    return monitor_check


@router.get(
    "organizations/{slug:organization_slug}/monitors/",
    response=list[MonitorSchema],
    by_alias=True,
)
@paginate(MonitorPagination)
async def list_monitors(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return get_monitor_queryset(request.auth.user_id, organization_slug)


@router.get(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/",
    response=MonitorDetailSchema,
    by_alias=True,
)
async def get_monitor(
    request: AuthHttpRequest, organization_slug: str, monitor_id: int
):
    monitor = await aget_object_or_404(
        get_monitor_queryset(request.auth.user_id, organization_slug),
        id=monitor_id,
    )
    await attach_checks_to_monitors([monitor])
    return monitor


@router.post(
    "organizations/{slug:organization_slug}/monitors/",
    response={201: MonitorSchema},
    by_alias=True,
)
async def create_monitor(
    request: AuthHttpRequest, organization_slug: str, payload: MonitorIn
):
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=user_id
    )
    data = payload.dict(exclude_defaults=True)
    if project_id := data.pop("project", None):
        data["project"] = await organization.projects.filter(id=project_id).afirst()
    monitor = await Monitor.objects.acreate(organization=organization, **data)
    monitor = await get_monitor_queryset(user_id, organization_slug).aget(id=monitor.id)
    await attach_checks_to_monitors([monitor])
    return 201, monitor


@router.put(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/",
    response=MonitorSchema,
    by_alias=True,
)
async def update_monitor(
    request: AuthHttpRequest,
    organization_slug: str,
    monitor_id: int,
    payload: MonitorIn,
):
    monitor = await aget_object_or_404(
        get_monitor_queryset(request.auth.user_id, organization_slug),
        id=monitor_id,
    )
    data = payload.dict()
    if project_id := data["project"]:
        result = await Project.objects.filter(
            organization__slug=organization_slug,
            organization__users=request.auth.user_id,
            id=project_id,
        ).afirst()
        data["project"] = result
    for attr, value in data.items():
        setattr(monitor, attr, value)
    await monitor.asave()
    await attach_checks_to_monitors([monitor])
    return monitor


@router.get(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/checks/",
    response=list[MonitorCheckResponseTimeSchema],
    by_alias=True,
)
@paginate
async def list_monitor_checks(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    monitor_id: int,
    is_change: bool | None = None,
):
    """
    List checks performed for a monitor
    Set is_change query param to True to show only changes,
    This is useful to see only when a service went up and down.
    """
    checks = (
        MonitorCheck.objects.filter(
            monitor_id=monitor_id,
            monitor__organization__slug=organization_slug,
            monitor__organization__users=request.auth.user_id,
        )
        .only("is_up", "start_check", "reason", "response_time")
        .order_by("-start_check")
    )
    if is_change is not None:
        checks = checks.filter(is_change=is_change)
    return checks


@router.get(
    "/organizations/{slug:organization_slug}/status-pages/",
    response=list[StatusPageSchema],
    by_alias=True,
)
@paginate
async def list_status_pages(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    """List status pages, used for showing the current status of an uptime monitor"""
    return StatusPage.objects.filter(
        organization__users=request.auth.user_id
    ).prefetch_related("monitors")


@router.post(
    "/organizations/{slug:organization_slug}/status-pages/",
    response={201: StatusPageSchema},
    by_alias=True,
)
async def create_status_page(
    request: AuthHttpRequest, organization_slug: str, payload: StatusPageIn
):
    organization = await aget_object_or_404(
        Organization, slug=organization_slug, users=request.auth.user_id
    )
    data = payload.dict()
    status_page = await StatusPage.objects.acreate(organization=organization, **data)
    return 201, await StatusPage.objects.prefetch_related("monitors").aget(
        id=status_page.id
    )


@router.delete(
    "organizations/{slug:organization_slug}/monitors/{int:monitor_id}/",
    response={204: None},
)
async def delete_monitor(
    request: AuthHttpRequest, organization_slug: str, monitor_id: int
):
    result, _ = (
        await get_monitor_queryset(request.auth.user_id, organization_slug)
        .filter(id=monitor_id)
        .adelete()
    )
    if result:
        return 204, None
    raise Http404

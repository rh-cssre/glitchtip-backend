from datetime import timedelta
from uuid import UUID

from django.db.models import F, Prefetch, Window
from django.db.models.functions import RowNumber
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.pagination import paginate

from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.utils import async_call_celery_task

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


def get_monitor_queryset(user_id: int, organization_slug: str):
    return (
        Monitor.objects.with_check_annotations()
        .filter(organization__users=user_id, organization__slug=organization_slug)
        # Fetch latest 60 checks for each monitor
        .prefetch_related(
            Prefetch(
                "checks",
                queryset=MonitorCheck.objects.filter(  # Optimization
                    start_check__gt=timezone.now() - timedelta(hours=12)
                )
                .annotate(
                    row_number=Window(
                        expression=RowNumber(),
                        order_by="-start_check",
                        partition_by=F("monitor"),
                    ),
                )
                .filter(row_number__lte=60)
                .distinct(),
            )
        )
        .select_related("project", "organization")
    )


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
        Monitor.objects.with_check_annotations(),
        organization__slug=organization_slug,
        endpoint_id=endpoint_id,
    )
    monitor_check = await MonitorCheck.objects.acreate(
        monitor=monitor,
        is_up=True,
        reason=None,
        is_change=monitor.latest_is_up is not True,
    )
    if monitor.latest_is_up is False:
        await async_call_celery_task(
            send_monitor_notification, monitor_check.pk, False, monitor.last_change
        )

    return monitor_check


@router.get(
    "organizations/{slug:organization_slug}/monitors/",
    response=list[MonitorSchema],
    by_alias=True,
)
@paginate
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
    return await aget_object_or_404(
        get_monitor_queryset(request.auth.user_id, organization_slug),
        id=monitor_id,
    )


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
    return 201, await get_monitor_queryset(user_id, organization_slug).aget(
        id=monitor.id
    )


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

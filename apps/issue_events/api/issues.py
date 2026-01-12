from collections import defaultdict
from datetime import timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from django.db.models import Count, Sum
from django.db.models.functions import TruncDay
from django.db.models.query import QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Query, Schema
from ninja.pagination import paginate

from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission

from ..constants import EventStatus
from ..models import Issue, IssueAggregate, IssueEvent, IssueHash
from ..schema import (
    IssueDetailSchema,
    IssueSchema,
    IssueStatsResponse,
    IssueTagSchema,
    StatsDetailSchema,
)
from ..services import IssueFilters, filter_issue_list, get_queryset, sort_options
from ..tasks import delete_issue_task, update_issues_task
from . import router

EventStatusEnum = StrEnum("EventStatusEnum", EventStatus.labels)


class UpdateIssueSchema(Schema):
    status: EventStatusEnum | None = None
    merge: int | None = None


@router.get(
    "/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_issue(request: AuthHttpRequest, issue_id: int):
    qs = await get_queryset(request.auth.user_id)
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        return await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()


@router.put(
    "/issues/{int:issue_id}/",
    response=IssueDetailSchema,
)
@has_permission(["event:write", "event:admin"])
async def update_issue(
    request: AuthHttpRequest,
    issue_id: int,
    payload: UpdateIssueSchema,
):
    qs = await get_queryset(request.auth.user_id)
    return await update_issue_status(qs, issue_id, payload)


@router.delete("/issues/{int:issue_id}/", response={204: None})
@has_permission(["event:write", "event:admin"])
async def delete_issue(request: AuthHttpRequest, issue_id: int):
    qs = await get_queryset(request.auth.user_id)
    result = await qs.filter(id=issue_id).aupdate(is_deleted=True)
    if not result:
        raise Http404()
    await delete_issue_task.aenqueue([issue_id])
    return 204, None


@router.put(
    "organizations/{slug:organization_slug}/issues/{int:issue_id}/",
    response=IssueDetailSchema,
)
@has_permission(["event:write", "event:admin"])
async def update_organization_issue(
    request: AuthHttpRequest,
    organization_slug: str,
    issue_id: int,
    payload: UpdateIssueSchema,
):
    qs = await get_queryset(request.auth.user_id, organization_slug=organization_slug)
    return await update_issue_status(qs, issue_id, payload)


async def update_issue_status(qs: QuerySet, issue_id: int, payload: UpdateIssueSchema):
    """
    BC Gitlab integration
    """
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        obj = await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()
    obj.status = EventStatus.from_string(payload.status)
    await obj.asave()
    return obj


@router.get(
    "organizations/{slug:organization_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
@paginate
async def list_issues(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[IssueFilters],
    sort: sort_options = "-last_seen",
):
    qs = (
        await get_queryset(request.auth.user_id, organization_slug=organization_slug)
    ).filter(is_deleted=False)
    event_id: UUID | None = None
    if filters.query:
        try:
            event_id = UUID(filters.query)
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
        except ValueError:
            pass
    return filter_issue_list(qs, filters, sort, event_id)


@router.delete(
    "organizations/{slug:organization_slug}/issues/", response=UpdateIssueSchema
)
@has_permission(["event:write", "event:admin"])
async def delete_issues(
    request: AuthHttpRequest,
    organization_slug: str,
    filters: Query[IssueFilters],
):
    qs = await get_queryset(request.auth.user_id, organization_slug=organization_slug)
    qs = filter_issue_list(qs, filters)
    await qs.aupdate(is_deleted=True)
    issue_ids = [
        issue_id
        async for issue_id in qs.filter(is_deleted=True).values_list("id", flat=True)
    ]
    await delete_issue_task.aenqueue(issue_ids)
    return {"status": "resolved"}


@router.put(
    "organizations/{slug:organization_slug}/issues/", response=UpdateIssueSchema
)
@has_permission(["event:write", "event:admin"])
async def update_issues(
    request: AuthHttpRequest,
    organization_slug: str,
    filters: Query[IssueFilters],
    payload: UpdateIssueSchema,
):
    user_id = request.auth.user_id
    qs = await get_queryset(user_id, organization_slug=organization_slug)
    qs = filter_issue_list(qs, filters)

    # Freeze the set of issues to update to avoid race conditions with new issues
    max_id = await qs.order_by("-id").values_list("id", flat=True).afirst()
    if not max_id:
        return payload

    qs = qs.filter(id__lte=max_id)

    # Process a limited batch immediately for UI responsiveness
    limit = 50
    updated_ids = [i async for i in qs.values_list("id", flat=True)[:limit]]

    should_enqueue = len(updated_ids) == limit
    task_kwargs = {
        "organization_slug": organization_slug,
        "user_id": user_id,
        "filter_params": filters.dict(),
        "exclude_ids": updated_ids,
        "max_id": max_id,
        "update_params": payload.dict(),
    }

    if payload.status:
        await Issue.objects.filter(id__in=updated_ids).aupdate(
            status=EventStatus.from_string(payload.status)
        )
        if should_enqueue:
            await update_issues_task.aenqueue(**task_kwargs)

    if payload.merge:
        # Identify the target issue (most recent one)
        # Note: logic requires that the target issue is in the initial queryset
        issue = await qs.order_by("-id").afirst()
        if not issue:
            return payload

        remove_qs = Issue.objects.filter(id__in=updated_ids).exclude(id=issue.id)
        await remove_qs.aupdate(is_deleted=True)
        await IssueHash.objects.filter(issue__in=remove_qs).aupdate(issue=issue)

        event_ids = []
        async for event_id in IssueEvent.objects.filter(
            issue__in=remove_qs
        ).values_list("id", flat=True)[:1000]:
            event_ids.append(event_id)
        await IssueEvent.objects.filter(id__in=event_ids).aupdate(issue=issue)

        if should_enqueue:
            # Pass the target merge issue ID to the task
            task_kwargs["update_params"]["merge"] = issue.id
            await update_issues_task.aenqueue(**task_kwargs)

    return payload


@router.get(
    "projects/{slug:organization_slug}/{slug:project_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
@paginate
async def list_project_issues(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    project_slug: str,
    filters: Query[IssueFilters],
    sort: sort_options = "-last_seen",
):
    qs = await get_queryset(
        request.auth.user_id,
        organization_slug=organization_slug,
        project_slug=project_slug,
    )
    event_id: UUID | None = None
    if filters.query:
        try:
            event_id = UUID(filters.query)
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
        except ValueError:
            pass
    return filter_issue_list(qs, filters, sort, event_id)


@router.get(
    "/issues/{int:issue_id}/tags/", response=list[IssueTagSchema], by_alias=True
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_issue_tags(
    request: AuthHttpRequest, issue_id: int, key: str | None = None
):
    qs = await get_queryset(request.auth.user_id)
    try:
        issue = await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()

    qs = issue.issuetag_set
    if key:
        qs = qs.filter(tag_key__key=key)
    qs = (
        qs.values("tag_key__key", "tag_value__value")
        .annotate(total_count=Sum("count"))
        .order_by("-total_count")[:100000]
    )
    keys = {row["tag_key__key"] async for row in qs}
    return [
        {
            "topValues": [
                {
                    "name": group["tag_value__value"],
                    "value": group["tag_value__value"],
                    "count": group["total_count"],
                    "key": group["tag_key__key"],
                }
                for group in qs
                if group["tag_key__key"] == key
            ],
            "uniqueValues": len(
                [group for group in qs if group["tag_key__key"] == key]
            ),
            "key": key,
            "name": key,
            "totalValues": sum(
                [group["total_count"] for group in qs if group["tag_key__key"] == key]
            ),
        }
        for key in keys
    ]


class IssueStatsFilters(Schema):
    groups: list[int]
    statsPeriod: Literal["14d", "24h"] = "24h"


@router.get(
    "organizations/{slug:organization_slug}/issues-stats/",
    response=list[IssueStatsResponse],
    summary="Retrieve Statistics for a Set of Issues",
    by_alias=True,
)
async def issue_stats(
    request: AuthHttpRequest, organization_slug: str, filters: Query[IssueStatsFilters]
):
    """
    Retrieves aggregated statistics for a given list of issue groups.

    This endpoint returns data for the last 24 hours, formatted as a series of
    [timestamp, count] pairs.
    """
    user_id = request.auth.user_id
    organization = await aget_object_or_404(
        Organization, users=user_id, slug=organization_slug
    )
    issues_qs = Issue.objects.filter(
        project__organization_id=organization.id, id__in=filters.groups
    )[:200]  # Sanity limit

    issue_list = [issue async for issue in issues_qs]
    issue_ids = [issue.id for issue in issue_list]

    if not issue_ids:
        return []

    is_24h = filters.statsPeriod == "24h"
    stats_map = defaultdict(list)

    if is_24h:
        # --- 24-Hour Period: Fetch hourly data ---
        start_date = timezone.now() - timedelta(hours=24)

        # Fetch pre-aggregated hourly stats from the last 24 hours.
        stats_qs = IssueAggregate.objects.filter(
            issue_id__in=issue_ids, date__gte=start_date
        ).values("issue_id", "date", "count")

        stats_list = [stat async for stat in stats_qs]

        # Group the hourly stats by issue_id.
        for stat in stats_list:
            timestamp = int(stat["date"].timestamp())
            stats_map[stat["issue_id"]].append([timestamp, stat["count"]])

        # Define a function to return the correct stats argument for the response.
        def get_stats_data(issue_id):
            return {"stats_24h": stats_map.get(issue_id, [])}

    else:
        # --- 14-Day Period: Fetch and group data by day ---
        start_date = timezone.now() - timedelta(days=14)

        # Fetch stats and aggregate them by day.
        daily_stats_qs = (
            IssueAggregate.objects.filter(issue_id__in=issue_ids, date__gte=start_date)
            .annotate(day=TruncDay("date"))
            .values("issue_id", "day")
            .annotate(daily_count=Sum("count"))
            .order_by("day")
        )

        daily_stats_list = [stat async for stat in daily_stats_qs]

        # Group the daily stats by issue_id.
        for stat in daily_stats_list:
            timestamp = int(stat["day"].timestamp())
            stats_map[stat["issue_id"]].append([timestamp, stat["daily_count"]])

        # Define a function to return the correct stats argument for the response.
        def get_stats_data(issue_id):
            return {"stats_14d": stats_map.get(issue_id, [])}

    return [
        IssueStatsResponse(
            id=str(issue.id),
            count=str(issue.count),
            user_count=issue.count,
            first_seen=issue.first_seen.isoformat(),
            last_seen=issue.last_seen.isoformat(),
            is_unhandled=issue.metadata.get("unhandled", False),
            stats=StatsDetailSchema(**get_stats_data(issue.id)),
        )
        for issue in issue_list
    ]

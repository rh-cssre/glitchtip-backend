import re
import shlex
from collections import defaultdict
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from django.db.models import Count, F, FloatField, Q, Sum, Value
from django.db.models.expressions import ExpressionWrapper
from django.db.models.functions import Extract, Log, TruncDay
from django.db.models.query import QuerySet
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Field, Query, Schema
from ninja.pagination import paginate
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated

from apps.organizations_ext.models import Organization
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.permissions import has_permission
from glitchtip.utils import async_call_celery_task

from ..constants import EventStatus, LogLevel
from ..models import Issue, IssueAggregate, IssueEvent, IssueHash
from ..schema import (
    IssueDetailSchema,
    IssueSchema,
    IssueStatsResponse,
    IssueTagSchema,
    StatsDetailSchema,
)
from ..tasks import delete_issue_task
from . import router


async def get_queryset(
    request: AuthHttpRequest,
    organization_slug: str | None = None,
    project_slug: str | None = None,
):
    user_id = request.auth.user_id
    qs = Issue.objects

    if organization_slug:
        organization = await aget_object_or_404(
            Organization, users=user_id, slug=organization_slug
        )
        qs = qs.filter(project__organization_id=organization.id)
    else:
        qs = qs.filter(project__organization__users=user_id)

    if project_slug:
        qs = qs.filter(project__slug=project_slug)
    return qs.annotate(
        num_comments=Count("comments", distinct=True),
    ).select_related("project")


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
    qs = await get_queryset(request)
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
    qs = await get_queryset(request)
    return await update_issue_status(qs, issue_id, payload)


@router.delete("/issues/{int:issue_id}/", response={204: None})
@has_permission(["event:write", "event:admin"])
async def delete_issue(request: AuthHttpRequest, issue_id: int):
    qs = await get_queryset(request)
    result = await qs.filter(id=issue_id).aupdate(is_deleted=True)
    if not result:
        raise Http404()
    await async_call_celery_task(delete_issue_task, [issue_id])
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
    qs = await get_queryset(request, organization_slug=organization_slug)
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


RELATIVE_TIME_REGEX = re.compile(r"now\s*\-\s*\d+\s*(m|h|d)\s*$")


def relative_to_datetime(v: Any) -> datetime:
    """
    Allow relative terms like now or now-1h. Only 0 or 1 subtraction operation is permitted.

    Accepts
    - now
    - - (subtraction)
    - m (minutes)
    - h (hours)
    - d (days)
    """
    result = timezone.now()
    if v == "now":
        return result
    if RELATIVE_TIME_REGEX.match(v):
        spaces_stripped = v.replace(" ", "")
        numbers = int(re.findall(r"\d+", spaces_stripped)[0])
        if spaces_stripped[-1] == "m":
            result -= timedelta(minutes=numbers)
        if spaces_stripped[-1] == "h":
            result -= timedelta(hours=numbers)
        if spaces_stripped[-1] == "d":
            result -= timedelta(days=numbers)
        return result
    return v


RelativeDateTime = Annotated[datetime, BeforeValidator(relative_to_datetime)]


class IssueFilters(Schema):
    id__in: list[int] | None = Field(None, alias="id")
    first_seen__gte: RelativeDateTime | None = Field(None, alias="start")
    first_seen__lte: RelativeDateTime | None = Field(None, alias="end")
    project__in: list[int] | None = Field(None, alias="project")
    environment: list[str] | None = None
    query: str | None = None


sort_options = Literal[
    "last_seen",
    "first_seen",
    "count",
    "priority",
    "-last_seen",
    "-first_seen",
    "-count",
    "-priority",
]


def filter_issue_list(
    qs: QuerySet,
    filters: Query[IssueFilters],
    sort: sort_options | None = None,
    event_id: UUID | None = None,
):
    qs_filters = filters.dict(exclude_none=True)
    query = qs_filters.pop("query", None)
    if filters.environment:
        qs_filters["issuetag__tag_key__key"] = "environment"
        qs_filters["issuetag__tag_value__value__in"] = qs_filters.pop("environment")
    if qs_filters:
        qs = qs.filter(**qs_filters)

    if event_id:
        qs = qs.filter(issueevent__id=event_id)
    elif query:
        queries = shlex.split(query)
        # First look for structured queries
        for i, query in enumerate(queries):
            query_part = query.split(":", 1)
            if len(query_part) == 2:
                query_name, query_value = query_part
                query_value = query_value.strip('"')

                if query_name == "is":
                    qs = qs.filter(status=EventStatus.from_string(query_value))
                elif query_name == "has":
                    # Does not require distinct as we already have a group by from annotations
                    qs = qs.filter(
                        issuetag__tag_key__key=query_value,
                    )
                elif query_name == "level":
                    qs = qs.filter(level=LogLevel.from_string(query_value))
                else:
                    qs = qs.filter(
                        issuetag__tag_key__key=query_name,
                        issuetag__tag_value__value=query_value,
                    )
            if len(query_part) == 1:
                search_query = " ".join(queries[i:])
                if "*" in search_query:
                    qs = qs.filter(
                        Q(title__ilike=f"%{search_query.replace('*', '%')}%")
                        | Q(search_vector=search_query)
                    )
                else:
                    qs = qs.filter(search_vector=search_query)
                # Search queries must be at end of query string, finished when parsing
                break

    if sort:
        if sort.endswith("priority"):
            # Inspired by https://stackoverflow.com/a/43788975/443457
            qs = qs.annotate(
                priority=ExpressionWrapper(
                    Log(10, F("count"))
                    + Extract(F("last_seen"), "epoch") / Value(300000.0),
                    output_field=FloatField(),
                )
            )
        qs = qs.order_by(sort)
    return qs


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
    qs = (await get_queryset(request, organization_slug=organization_slug)).filter(
        is_deleted=False
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


@router.delete(
    "organizations/{slug:organization_slug}/issues/", response=UpdateIssueSchema
)
@has_permission(["event:write", "event:admin"])
async def delete_issues(
    request: AuthHttpRequest,
    organization_slug: str,
    filters: Query[IssueFilters],
):
    qs = await get_queryset(request, organization_slug=organization_slug)
    qs = filter_issue_list(qs, filters)
    await qs.aupdate(is_deleted=True)
    issue_ids = [
        issue_id
        async for issue_id in qs.filter(is_deleted=True).values_list("id", flat=True)
    ]
    await async_call_celery_task(delete_issue_task, issue_ids)
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
    qs = await get_queryset(request, organization_slug=organization_slug)
    qs = filter_issue_list(qs, filters)
    if payload.status:
        await qs.aupdate(status=EventStatus.from_string(payload.status))
    if payload.merge:
        issue = await qs.order_by("-id").afirst()
        if not issue:
            return payload
        remove_qs = qs.exclude(id=issue.id)
        await remove_qs.aupdate(is_deleted=True)
        await IssueHash.objects.filter(issue__in=remove_qs).aupdate(issue=issue)
        # Switch only the first 1000 events
        event_ids = []
        async for event_id in IssueEvent.objects.filter(
            issue__in=remove_qs
        ).values_list("id", flat=True)[:1000]:
            event_ids.append(event_id)
        await IssueEvent.objects.filter(id__in=event_ids).aupdate(issue=issue)
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
        request, organization_slug=organization_slug, project_slug=project_slug
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
    qs = await get_queryset(request)
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

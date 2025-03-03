import re
import shlex
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Optional
from uuid import UUID

from django.db.models import Count, Sum
from django.db.models.expressions import RawSQL
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
from ..models import Issue
from ..schema import IssueDetailSchema, IssueSchema, IssueTagSchema
from ..tasks import delete_issue_task
from . import router


async def get_queryset(
    request: AuthHttpRequest,
    organization_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
):
    user_id = request.auth.user_id
    qs = Issue.objects.all()

    if organization_slug:
        organization = await aget_object_or_404(
            Organization, users=user_id, slug=organization_slug
        )
        qs = qs.filter(project__organization_id=organization.id)
    else:
        qs = qs.filter(project__organization__users=user_id)

    if project_slug:
        qs = qs.filter(project__slug=project_slug)
    qs = qs.annotate(
        num_comments=Count("comments", distinct=True),
    )
    return qs.select_related("project")


EventStatusEnum = StrEnum("EventStatusEnum", EventStatus.labels)


class UpdateIssueSchema(Schema):
    status: EventStatusEnum


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
    id__in: list[int] = Field(None, alias="id")
    first_seen__gte: RelativeDateTime = Field(None, alias="start")
    first_seen__lte: RelativeDateTime = Field(None, alias="end")
    project__in: list[str] = Field(None, alias="project")
    environment: Optional[list[str]] = None
    query: Optional[str] = None


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
    sort: Optional[sort_options] = None,
    event_id: Optional[UUID] = None,
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
                qs = qs.filter(search_vector=search_query)
                # Search queries must be at end of query string, finished when parsing
                break

    if sort:
        if sort.endswith("priority"):
            # Raw SQL must be added when sorting by priority
            # Inspired by https://stackoverflow.com/a/43788975/443457
            qs = qs.annotate(
                priority=RawSQL(
                    "LOG10(count) + EXTRACT(EPOCH FROM last_seen)/300000", ()
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
    qs = await get_queryset(request, organization_slug=organization_slug)
    event_id: Optional[UUID] = None
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
    await qs.aupdate(status=EventStatus.from_string(payload.status))
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
    event_id: Optional[UUID] = None
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
    request: AuthHttpRequest, issue_id: int, key: Optional[str] = None
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

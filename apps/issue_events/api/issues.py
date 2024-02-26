import re
import shlex
from datetime import datetime, timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from django.db.models import Count, Sum
from django.db.models.expressions import RawSQL
from django.db.models.query import QuerySet
from django.http import Http404, HttpResponse
from django.utils import timezone
from ninja import Field, Query, Schema
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated

from events.models import LogLevel
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate
from glitchtip.api.permissions import has_permission
from organizations_ext.models import Organization

from ..constants import EventStatus
from ..models import Issue
from ..schema import IssueDetailSchema, IssueSchema, IssueTagSchema
from . import router


async def get_queryset(
    request: AuthHttpRequest,
    organization_slug: Optional[str] = None,
    project_slug: Optional[str] = None,
):
    user_id = request.auth.user_id
    qs = Issue.objects.all()

    if organization_slug:
        organization = await Organization.objects.filter(
            users=user_id, slug=organization_slug
        ).afirst()
        qs = qs.filter(project__organization_id=organization.id)
    else:
        qs = qs.filter(project__organization__users=user_id)

    if project_slug:
        qs = qs.filter(project__slug=project_slug)
    qs = qs.annotate(
        num_comments=Count("comments", distinct=True),
    )
    return qs.select_related("project")


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
    first_seen__gte: RelativeDateTime = Field(None, alias="start")
    first_seen__lte: RelativeDateTime = Field(None, alias="end")
    project__in: list[str] = Field(None, alias="project")


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
    sort: sort_options,
    query: Optional[str] = None,
    event_id: Optional[UUID] = None,
    environment: Optional[list[str]] = Query(None),
):
    if environment:
        qs = qs.filter(
            issuetag__tag_key__key="environment",
            issuetag__tag_value__value__in=environment,
        )
    if qs_filters := filters.dict(exclude_none=True):
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
                    qs = qs.filter(
                        level=LogLevel.from_string(query_value)
                    )
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

    if sort.endswith("priority"):
        # Raw SQL must be added when sorting by priority
        # Inspired by https://stackoverflow.com/a/43788975/443457
        qs = qs.annotate(
            priority=RawSQL("LOG10(count) + EXTRACT(EPOCH FROM last_seen)/300000", ())
        )

    return qs.order_by(sort)


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
    query: Optional[str] = None,
    sort: sort_options = "-last_seen",
    environment: Optional[list[str]] = Query(None),
):
    qs = await get_queryset(request, organization_slug=organization_slug)
    event_id: Optional[UUID] = None
    if query:
        try:
            event_id = UUID(query)
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
        except ValueError:
            pass
    return filter_issue_list(qs, filters, sort, query, event_id, environment)


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
    query: Optional[str] = None,
    sort: sort_options = "-last_seen",
    environment: Optional[list[str]] = Query(None),
):
    qs = await get_queryset(
        request, organization_slug=organization_slug, project_slug=project_slug
    )
    event_id: Optional[UUID] = None
    if query:
        try:
            event_id = UUID(query)
            request.matching_event_id = event_id
            response["X-Sentry-Direct-Hit"] = "1"
        except ValueError:
            pass
    return filter_issue_list(qs, filters, sort, query, event_id, environment)


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

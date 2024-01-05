import shlex
from datetime import datetime
from typing import Literal, Optional

from django.db.models import Count
from django.db.models.expressions import RawSQL
from django.http import Http404, HttpResponse
from ninja import Field, Query, Schema

from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import paginate

from ..constants import EventStatus
from ..models import Issue
from ..schema import IssueDetailSchema, IssueSchema
from . import router


def get_queryset(request: AuthHttpRequest, organization_slug: Optional[str] = None):
    user_id = request.auth.user_id
    qs = Issue.objects.filter(project__organization__users=user_id)
    if organization_slug:
        qs = qs.filter(project__organization__slug=organization_slug)
    qs = qs.annotate(
        num_comments=Count("comments", distinct=True),
    ).select_related("project")
    return qs


@router.get(
    "/issues/{int:issue_id}/",
    response=IssueDetailSchema,
    by_alias=True,
)
async def get_issue(request: AuthHttpRequest, issue_id: int):
    qs = get_queryset(request)
    qs = qs.annotate(
        user_report_count=Count("userreport", distinct=True),
    )
    try:
        return await qs.filter(id=issue_id).aget()
    except Issue.DoesNotExist:
        raise Http404()


class IssueFilters(Schema):
    first_seen__gte: datetime = Field(None, alias="start")
    first_seen__lte: datetime = Field(None, alias="end")
    project__in: list[str] = Field(None, alias="project")
    tags__environment__has_any_keys: list[str] = Field(None, alias="environment")


@router.get(
    "organizations/{slug:organization_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
@paginate
async def list_issues(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[IssueFilters],
    query: Optional[str] = None,
    sort: Literal[
        "last_seen",
        "first_seen",
        "count",
        "priority",
        "-last_seen",
        "-first_seen",
        "-count",
        "-priority",
    ] = "-last_seen",
    environment: Optional[list[str]] = None,
):
    qs = get_queryset(request, organization_slug=organization_slug)
    if qs_filters := filters.dict(exclude_none=True):
        qs = qs.filter(**qs_filters)
    if query:
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
                    qs = qs.filter(tags__has_key=query_value)
                else:
                    qs = qs.filter(tags__contains={query_name: [query_value]})
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
    # return [obj async for obj in qs]

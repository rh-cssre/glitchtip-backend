import shlex
from datetime import datetime
from typing import Optional

from django.db.models import Count
from django.db.models.expressions import RawSQL
from django.http import Http404

from glitchtip.api.authentication import AuthHttpRequest

from ..constants import EventStatus
from ..models import Issue
from ..schema import IssueDetailSchema, IssueSchema
from . import router


def get_queryset(request: AuthHttpRequest, organization_slug: Optional[str] = None):
    user_id = request.auth
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


@router.get(
    "organizations/{slug:organization_slug}/issues/",
    response=list[IssueSchema],
    by_alias=True,
)
async def list_issues(
    request: AuthHttpRequest,
    organization_slug: str,
    query: Optional[str] = None,
    sort: Optional[str] = None,
    environment: Optional[list[str]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
):
    qs = get_queryset(request, organization_slug=organization_slug)
    if start:
        # Should really be events, not first seen
        qs = qs.filter(first_seen__gte=start)
    if end:
        qs = qs.filter(first_seen__lte=end)
    if environment:
        qs = qs.filter(tags__environment__has_any_keys=environment)
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
        if sort and sort.endswith("priority"):
            # Raw SQL must be added when sorting by priority
            # Inspired by https://stackoverflow.com/a/43788975/443457
            qs = qs.annotate(
                priority=RawSQL(
                    "LOG10(count) + EXTRACT(EPOCH FROM last_seen)/300000", ()
                )
            )

    return [obj async for obj in qs]

import re
import shlex
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from django.db.models import Count, F, FloatField, Q, Value
from django.db.models.expressions import ExpressionWrapper
from django.db.models.functions import Extract, Log
from django.db.models.query import QuerySet
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Field, Schema
from pydantic.functional_validators import BeforeValidator
from typing_extensions import Annotated

from apps.organizations_ext.models import Organization

from .constants import EventStatus, LogLevel
from .models import Issue

RELATIVE_TIME_REGEX = re.compile(r"now\s*\-\s*\d+\s*(m|h|d)\s*$")


def is_uuid7(u: UUID) -> bool:
    """Check if a UUID has version 7 (RFC 9562) based on the version nibble."""
    return u.version == 7


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
    if isinstance(v, str) and RELATIVE_TIME_REGEX.match(v):
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

# count/last_seen moved to the IssueIndex leaf; map the public sort keys to
# their ORM path. first_seen stays on Issue; priority is an annotation.
_SORT_FIELD_MAP = {
    "last_seen": "index__last_seen",
    "count": "index__count",
}


async def get_queryset(
    user_id: int | None,
    organization_slug: str | None = None,
    project_slug: str | None = None,
) -> QuerySet[Issue]:
    qs = Issue.objects
    leaf_org_id: int | None = None

    if organization_slug:
        if user_id:
            organization = await aget_object_or_404(
                Organization, users=user_id, slug=organization_slug
            )
            qs = qs.filter(project__organization_id=organization.id)
            leaf_org_id = organization.id
        else:
            # Internal/System usage without user_id
            qs = qs.filter(project__organization__slug=organization_slug)
    elif user_id:
        qs = qs.filter(project__organization__users=user_id)

    if project_slug:
        qs = qs.filter(project__slug=project_slug)

    # Constrain the IssueIndex partition key so org-scoped list/search
    # queries prune the hash partitions instead of scanning all of them (the
    # leaf is joined on issue_id, which alone gives the planner no partition to
    # pick). Every non-deleted issue has a leaf row, so this never drops results.
    if leaf_org_id is not None:
        qs = qs.filter(index__organization_id=leaf_org_id)

    return qs.annotate(
        num_comments=Count("comments", distinct=True),
    ).select_related(
        "project",
        "first_release",
        "resolved_in_release",
        "assigned_to_org_user__user",
        "assigned_to_team",
        # The hot columns (count/last_seen/status/level/last_release) live on the
        # IssueIndex leaf; pull it (and its last_release) so the Issue proxy
        # properties and the serializer don't issue a query per row.
        "index__last_release",
    )


def filter_issue_list(
    qs: QuerySet[Issue],
    filters: IssueFilters,
    sort: sort_options | None = None,
    event_id: UUID | None = None,
    organization_id: int | None = None,
) -> QuerySet[Issue]:
    # Handle both Pydantic model and dict
    if isinstance(filters, dict):
        qs_filters = filters.copy()
    else:
        qs_filters = filters.dict(exclude_none=True)

    query = qs_filters.pop("query", None)

    environment = qs_filters.pop("environment", None)
    if environment:
        qs_filters["issuetag__tag_key__key"] = "environment"
        qs_filters["issuetag__tag_value__value__in"] = environment

    if qs_filters:
        qs = qs.filter(**qs_filters)

    if event_id:
        if is_uuid7(event_id):
            # UUIDv7 id — prunes to a single time-range partition
            qs = qs.filter(issueevent__id=event_id)
        else:
            # Client-provided sentry SDK event_id (typically UUIDv4).
            # Must scan event_id index across all time partitions.
            # Include organization_id to prune hash sub-partitions.
            event_filter = Q(issueevent__event_id=event_id)
            if organization_id:
                event_filter &= Q(issueevent__organization_id=organization_id)
            qs = qs.filter(event_filter)
    elif query:
        try:
            queries = shlex.split(query)
        except ValueError:
            queries = query.split()
        # First look for structured queries
        for i, query in enumerate(queries):
            query_part = query.split(":", 1)
            if len(query_part) == 2:
                query_name, query_value = query_part
                query_value = query_value.strip('"')

                if query_name == "is":
                    qs = qs.filter(index__status=EventStatus.from_string(query_value))
                elif query_name == "has":
                    # Does not require distinct as we already have a group by from annotations
                    qs = qs.filter(
                        issuetag__tag_key__key=query_value,
                    )
                elif query_name == "level":
                    qs = qs.filter(index__level=LogLevel.from_string(query_value))
                else:
                    qs = qs.filter(
                        issuetag__tag_key__key=query_name,
                        issuetag__tag_value__value=query_value,
                    )
            if len(query_part) == 1:
                search_query = " ".join(queries[i:])
                # Full-text search reads the decoupled IssueIndex (the
                # Issue.search_vector column has been dropped). Scoping by
                # organization_id lets Postgres prune the hash partitions;
                # without it the join is on issue_id alone and every partition
                # is scanned, so callers must resolve organization_id on the
                # text-search path (list_issues / list_project_issues do).
                index_q = Q(index__fts_document=search_query)
                if organization_id:
                    index_q &= Q(index__organization_id=organization_id)
                if "*" in search_query:
                    qs = qs.filter(
                        Q(title__ilike=f"%{search_query.replace('*', '%')}%") | index_q
                    )
                else:
                    qs = qs.filter(index_q)
                # Search queries must be at end of query string, finished when parsing
                break

    if sort:
        if sort.endswith("priority"):
            # Inspired by https://stackoverflow.com/a/43788975/443457
            qs = qs.annotate(
                priority=ExpressionWrapper(
                    Log(10, F("index__count"))
                    + Extract(F("index__last_seen"), "epoch") / Value(300000.0),
                    output_field=FloatField(),
                )
            )
        # count/last_seen live on the IssueIndex leaf; map the sort key to
        # the relation path (first_seen and priority stay as-is).
        descending = sort.startswith("-")
        key = sort.lstrip("-")
        order_field = _SORT_FIELD_MAP.get(key, key)
        qs = qs.order_by(("-" if descending else "") + order_field)
    return qs

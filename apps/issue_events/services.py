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


async def get_queryset(
    user_id: int | None,
    organization_slug: str | None = None,
    project_slug: str | None = None,
) -> QuerySet[Issue]:
    qs = Issue.objects

    if organization_slug:
        if user_id:
            organization = await aget_object_or_404(
                Organization, users=user_id, slug=organization_slug
            )
            qs = qs.filter(project__organization_id=organization.id)
        else:
            # Internal/System usage without user_id
            qs = qs.filter(project__organization__slug=organization_slug)
    elif user_id:
        qs = qs.filter(project__organization__users=user_id)

    if project_slug:
        qs = qs.filter(project__slug=project_slug)

    return qs.annotate(
        num_comments=Count("comments", distinct=True),
    ).select_related("project")


def filter_issue_list(
    qs: QuerySet[Issue],
    filters: IssueFilters,
    sort: sort_options | None = None,
    event_id: UUID | None = None,
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

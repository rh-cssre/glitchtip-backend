from datetime import datetime
from typing import Any, Literal

from django.db.models import Avg, Count
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Query, Router, Schema
from ninja.pagination import paginate

from apps.shared.schema.fields import RelativeDateTime
from glitchtip.api.authentication import AuthHttpRequest

from .models import TransactionEvent, TransactionGroup
from .schema import TransactionEventSchema, TransactionGroupSchema

router = Router()


def get_transaction_group_queryset(
    organization_slug: str, start: datetime | None = None, end: datetime | None = None
):
    qs = TransactionGroup.objects.filter(project__organization__slug=organization_slug)
    filter_kwargs: dict[str, Any] = {}
    if start:
        filter_kwargs["transactionevent__start_timestamp__gte"] = start
    if end:
        filter_kwargs["transactionevent__start_timestamp__lte"] = end
    if filter_kwargs:
        qs = qs.filter(**filter_kwargs)

    return qs.annotate(
        avg_duration=Avg("transactionevent__duration"),
        transaction_count=Count("transactionevent"),
    )


@router.get(
    "organizations/{slug:organization_slug}/transactions/",
    response=list[TransactionEventSchema],
)
@paginate
async def list_transactions(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str
):
    return TransactionEvent.objects.filter(
        group__project__organization__slug=organization_slug
    ).order_by("start_timestamp")


class TransactionGroupFilters(Schema):
    start: RelativeDateTime | None = None
    end: RelativeDateTime | None = None
    sort: Literal[
        "created",
        "-created",
        "avg_duration",
        "-avg_duration",
        "transaction_count",
        "-transaction_count",
    ] = "-avg_duration"
    environment: list[str] = []
    query: str | None = None


@router.get(
    "organizations/{slug:organization_slug}/transaction-groups/",
    response=list[TransactionGroupSchema],
    by_alias=True,
)
@paginate
async def list_transaction_groups(
    request: AuthHttpRequest,
    response: HttpResponse,
    filters: Query[TransactionGroupFilters],
    organization_slug: str,
):
    queryset = get_transaction_group_queryset(
        organization_slug, start=filters.start, end=filters.end
    )
    if filters.environment:
        queryset = queryset.filter(tags__environment__has_any_keys=filters.environment)
    return queryset.order_by(filters.sort)


@router.get(
    "organizations/{slug:organization_slug}/transaction-groups/{int:id}/",
    response=TransactionGroupSchema,
    by_alias=True,
)
async def get_transaction_group(
    request: AuthHttpRequest, response: HttpResponse, organization_slug: str, id: int
):
    return await aget_object_or_404(
        get_transaction_group_queryset(organization_slug), id=id
    )

from datetime import timedelta
from typing import Literal

from asgiref.sync import sync_to_async
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import Query, Router, Schema

from apps.organizations_ext.models import Organization
from apps.shared.schema.fields import RelativeDateTime
from glitchtip.api.permissions import (
    OrgScopedHttpRequest,
    org_scoped,
    org_scoped_list,
)

from .cold_storage import (
    query_n_plus_one_patterns,
    query_span_groups,
    query_span_groups_for_transaction,
    query_transaction_trend,
)
from .models import TransactionGroup
from .schema import (
    NPlusOnePatternSchema,
    SpanGroupSchema,
    TransactionGroupSchema,
    TransactionTrendSchema,
)

router = Router()


class TransactionGroupFilters(Schema):
    start: RelativeDateTime | None = None
    end: RelativeDateTime | None = None
    sort: Literal[
        "created",
        "-created",
        "avg_duration",
        "-avg_duration",
        "count",
        "-count",
    ] = "-avg_duration"
    project: list[int] = []
    query: str | None = None


class SpanGroupFilters(Schema):
    start: RelativeDateTime | None = None
    end: RelativeDateTime | None = None


class OrgSpanGroupFilters(Schema):
    start: RelativeDateTime | None = None
    end: RelativeDateTime | None = None
    project: list[int] = []
    op: str | None = None
    sort: Literal[
        "total_time",
        "-total_time",
        "avg_duration",
        "-avg_duration",
        "count",
        "-count",
    ] = "-total_time"
    limit: int = 50


class NPlusOneFilters(Schema):
    start: RelativeDateTime | None = None
    end: RelativeDateTime | None = None
    project: list[int] = []
    op: str | None = "db"
    threshold: float = 5.0
    limit: int = 50


@router.get(
    "organizations/{slug:organization_id_or_slug}/transaction-groups/",
    response=list[TransactionGroupSchema],
    by_alias=True,
)
@org_scoped_list(["event:read", "event:write", "event:admin"], TransactionGroup)
async def list_transaction_groups(
    request: OrgScopedHttpRequest,
    response: HttpResponse,
    filters: Query[TransactionGroupFilters],
    organization_id_or_slug: str,
):
    qs = TransactionGroup.objects.all()

    if filters.project:
        qs = qs.filter(project_id__in=filters.project)
    if filters.start:
        qs = qs.filter(last_seen__gte=filters.start)
    if filters.end:
        qs = qs.filter(last_seen__lte=filters.end)
    if filters.query:
        qs = qs.filter(transaction__icontains=filters.query)

    return qs.order_by(filters.sort)


@router.get(
    "organizations/{slug:organization_id_or_slug}/transaction-groups/{int:id}/",
    response=TransactionGroupSchema,
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], TransactionGroup)
async def get_transaction_group(
    request: OrgScopedHttpRequest,
    qs: QuerySet[TransactionGroup],
    organization_id_or_slug: str,
    id: int,
):
    return await aget_object_or_404(qs, id=id)


@router.get(
    "organizations/{slug:organization_id_or_slug}/transaction-groups/{int:id}/spans/",
    response=list[SpanGroupSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], TransactionGroup)
async def list_transaction_spans(
    request: OrgScopedHttpRequest,
    qs: QuerySet[TransactionGroup],
    organization_id_or_slug: str,
    id: int,
    filters: Query[SpanGroupFilters],
):
    group = await qs.filter(id=id).afirst()
    if not group:
        return []

    now = timezone.now()
    start_dt = filters.start or (now - timedelta(days=7))
    end_dt = filters.end or now

    return await sync_to_async(query_span_groups_for_transaction)(
        org_id=group.organization_id,
        transaction_name=group.transaction,
        start_dt=start_dt,
        end_dt=end_dt,
    )


@router.get(
    "organizations/{slug:organization_id_or_slug}/span-groups/",
    response=list[SpanGroupSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def list_span_groups(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    filters: Query[OrgSpanGroupFilters],
):
    # Cold storage can't carry the gate, so prove membership first.
    organization = await qs.only("id").afirst()
    if not organization:
        return []

    now = timezone.now()
    start_dt = filters.start or (now - timedelta(days=7))
    end_dt = filters.end or now
    project_ids = filters.project or None

    return await sync_to_async(query_span_groups)(
        org_id=organization.id,
        project_ids=project_ids,
        start_dt=start_dt,
        end_dt=end_dt,
        op_filter=filters.op,
        sort=filters.sort,
        limit=min(filters.limit, 100),
    )


@router.get(
    "organizations/{slug:organization_id_or_slug}/n-plus-one/",
    response=list[NPlusOnePatternSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], Organization)
async def list_n_plus_one_patterns(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    filters: Query[NPlusOneFilters],
):
    # Cold storage can't carry the gate, so prove membership first.
    organization = await qs.only("id").afirst()
    if not organization:
        return []

    now = timezone.now()
    start_dt = filters.start or (now - timedelta(days=7))
    end_dt = filters.end or now
    project_ids = filters.project or None

    return await sync_to_async(query_n_plus_one_patterns)(
        org_id=organization.id,
        project_ids=project_ids,
        start_dt=start_dt,
        end_dt=end_dt,
        op_filter=filters.op,
        threshold=filters.threshold,
        limit=min(filters.limit, 100),
    )


@router.get(
    "organizations/{slug:organization_id_or_slug}/transaction-groups/{int:id}/trend/",
    response=list[TransactionTrendSchema],
    by_alias=True,
)
@org_scoped(["event:read", "event:write", "event:admin"], TransactionGroup)
async def get_transaction_trend(
    request: OrgScopedHttpRequest,
    qs: QuerySet[TransactionGroup],
    organization_id_or_slug: str,
    id: int,
    filters: Query[SpanGroupFilters],
):
    group = await qs.filter(id=id).afirst()
    if not group:
        return []

    now = timezone.now()
    start_dt = filters.start or (now - timedelta(days=7))
    end_dt = filters.end or now

    return await sync_to_async(query_transaction_trend)(
        org_id=group.organization_id,
        transaction_name=group.transaction,
        start_dt=start_dt,
        end_dt=end_dt,
    )

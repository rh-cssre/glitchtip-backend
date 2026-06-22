import asyncio
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Prefetch, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.http import JsonResponse
from django.shortcuts import aget_object_or_404
from django.utils import timezone
from ninja import ModelSchema, Router

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import (
    EventCounts,
    Organization,
    get_current_period_dates,
    get_event_counts,
)
from apps.organizations_ext.tasks import check_organization_throttle
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    LogProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)
from apps.uptime.models import UptimeCheckHourlyStatistic
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.schema import CamelSchema

from .client import (
    create_customer,
    create_portal_session,
    create_session,
    create_subscription,
)
from .constants import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CollectionMethod,
    SubscriptionStatus,
)
from .models import StripePrice, StripeProduct, StripeSubscription
from .utils import compute_cycle, unix_to_datetime

router = Router()


class StripeIDSchema(CamelSchema):
    stripe_id: str


class StripeNestedPriceSchema(StripeIDSchema, ModelSchema):
    price: str

    class Meta:
        model = StripePrice
        fields = ["price", "interval"]

    @staticmethod
    def resolve_price(obj: StripePrice):
        return str(obj.price)


class StripeProductSchema(StripeIDSchema, ModelSchema):
    class Meta:
        model = StripeProduct
        fields = ["name", "description", "events", "default_price"]


class StripeProductExpandedPriceSchema(StripeIDSchema, ModelSchema):
    default_price: StripeNestedPriceSchema
    prices: list[StripeNestedPriceSchema]
    marketing_features: list[str]

    class Meta:
        model = StripeProduct
        fields = ["name", "description", "events"]

    @staticmethod
    def resolve_default_price(obj: StripeProduct):
        return obj.default_price

    @staticmethod
    def resolve_prices(obj: StripeProduct):
        return obj.prices_list  # type: ignore[attr-defined]


class StripeSubscriptionSchema(StripeIDSchema, ModelSchema):
    product: StripeProductSchema
    price: StripeNestedPriceSchema
    status: SubscriptionStatus | None
    collection_method: CollectionMethod

    class Meta:
        model = StripeSubscription
        fields = [
            "created",
            "current_period_start",
            "current_period_end",
            "start_date",
            "subscription_cycle_start",
            "subscription_cycle_end",
        ]

    @staticmethod
    def resolve_price(obj: StripeSubscription):
        return obj.price

    @staticmethod
    def resolve_product(obj: StripeSubscription):
        return obj.price.product

    @staticmethod
    def resolve_subscription_cycle_start(obj: StripeSubscription):
        return obj.subscription_cycle_start or obj.current_period_start

    @staticmethod
    def resolve_subscription_cycle_end(obj: StripeSubscription):
        return obj.subscription_cycle_end or obj.current_period_end


class PriceIDSchema(CamelSchema):
    price: str


class SubscriptionIn(PriceIDSchema):
    organization: str


class CreateSubscriptionResponse(SubscriptionIn):
    subscription: StripeSubscriptionSchema


class StripeCheckoutSessionSchema(CamelSchema):
    url: str


class StripePortalSessionSchema(CamelSchema):
    url: str


# Per-category fields are unfloored billed contributions (uptime/logs are floats);
# `total` is the once-floored billed integer. Clients display these as-is.
class SubscriptionUsageSchema(CamelSchema):
    total: int
    event_count: int
    transaction_event_count: int
    uptime_check_event_count: float
    log_event_count: float
    file_size_mb: int


class DailyEventCountEntry(CamelSchema):
    date: date
    event_count: int
    transaction_event_count: int
    uptime_check_event_count: float
    log_event_count: float


class DailyEventsCountSchema(CamelSchema):
    data: list[DailyEventCountEntry]


@router.get("products/", response=list[StripeProductExpandedPriceSchema], by_alias=True)
async def list_stripe_products(request: AuthHttpRequest):
    products = (
        StripeProduct.objects.filter(is_public=True, events__gt=0)
        .select_related("default_price")
        .prefetch_related(
            Prefetch(
                "stripeprice_set",
                queryset=StripePrice.objects.filter(is_public=True),
                to_attr="prices_list",
            )
        )
    )
    result = []
    async for product in products:
        result.append(product)
    return result


@router.get(
    "subscriptions/{slug:organization_slug}/",
    response=StripeSubscriptionSchema | None,
    by_alias=True,
)
async def get_stripe_subscription(request: AuthHttpRequest, organization_slug: str):
    return await (
        StripeSubscription.objects.filter(
            organization__users=request.auth.user_id,
            organization__slug=organization_slug,
            status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        )
        .select_related("price__product")
        .order_by("-created")
        .afirst()
    )


@router.post(
    "organizations/{slug:organization_slug}/create-stripe-subscription-checkout/",
    response=StripeCheckoutSessionSchema,
)
async def create_stripe_session(
    request: AuthHttpRequest, organization_slug: str, payload: PriceIDSchema
):
    """
    Create Stripe Checkout, send to client for redirecting to Stripe
    See https://stripe.com/docs/api/checkout/sessions/create
    """
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    # Ensure price exists
    price_id = payload.price
    await aget_object_or_404(StripePrice, stripe_id=price_id)
    return await create_session(price_id, customer_id, organization_slug)


@router.post(
    "organizations/{slug:organization_slug}/create-billing-portal/",
    response=StripePortalSessionSchema,
)
async def stripe_billing_portal_session(
    request: AuthHttpRequest, organization_slug: str
):
    """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        slug=organization_slug,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    return await create_portal_session(customer_id, organization_slug)


@router.post("subscriptions/", response=CreateSubscriptionResponse, by_alias=True)
async def stripe_create_subscription(request: AuthHttpRequest, payload: SubscriptionIn):
    org_id = int(payload.organization)
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        id=org_id,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    price = await aget_object_or_404(
        StripePrice.objects.select_related("product"), stripe_id=payload.price, price=0
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    if await StripeSubscription.objects.filter(
        organization=organization, status__in=ACTIVE_SUBSCRIPTION_STATUSES
    ).aexists():
        return JsonResponse({"detail": "Customer already has subscription"}, status=400)
    subscription_resp = await create_subscription(customer_id, price.stripe_id)
    current_period_start = unix_to_datetime(
        subscription_resp.items.data[0].current_period_start
    )
    current_period_end = unix_to_datetime(
        subscription_resp.items.data[0].current_period_end
    )
    price_data = subscription_resp.items.data[0].price
    is_annual = bool(
        price_data.recurring and price_data.recurring.get("interval") == "year"
    )
    cycle_start, cycle_end = compute_cycle(
        current_period_start, current_period_end, is_annual
    )
    subscription = await StripeSubscription.objects.acreate(
        stripe_id=subscription_resp.id,
        status=SubscriptionStatus.ACTIVE,
        created=unix_to_datetime(subscription_resp.created),
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        start_date=unix_to_datetime(subscription_resp.start_date),
        collection_method=subscription_resp.collection_method,
        subscription_cycle_start=cycle_start,
        subscription_cycle_end=cycle_end,
        price=price,
        organization=organization,
    )
    organization.stripe_primary_subscription = subscription
    await organization.asave(update_fields=["stripe_primary_subscription"])
    await check_organization_throttle.aenqueue(organization.id)
    return {
        "price": price.stripe_id,
        "organization": str(organization.id),
        "subscription": subscription,
    }


def usage_response(counts: EventCounts) -> dict:
    """Usage payload: per-category billed via billed(), total once-floored."""
    return {
        "total": counts.total_event_count,
        "event_count": counts.issue_event_count,
        "transaction_event_count": counts.transaction_count,
        "uptime_check_event_count": counts.billed("uptime_check_event_count"),
        "log_event_count": counts.billed("log_count"),
        "file_size_mb": counts.file_size,
    }


@router.get(
    "subscriptions/{slug:organization_slug}/events_count/period/",
    response=SubscriptionUsageSchema,
    by_alias=True,
)
async def subscription_events_count_for_period(
    request: AuthHttpRequest,
    organization_slug: str,
    periods_ago: int = 0,
):
    retention_days = max(30, settings.GLITCHTIP_RETENTION_DAYS)
    if periods_ago * 30 > retention_days:
        return JsonResponse(
            {
                "detail": f"periods_ago exceeds data retention limit ({retention_days} days)"
            },
            status=400,
        )

    org = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=request.auth.user_id,
    )

    period = await get_current_period_dates(org, periods_ago)
    if period is None:
        # periods_ago precedes an annual subscription's start.
        return usage_response(EventCounts())

    start, end = period
    counts = await get_event_counts(org.id, start, end)
    return usage_response(counts)


@router.get(
    "subscriptions/{slug:organization_slug}/events_count/daily/",
    response=DailyEventsCountSchema,
    by_alias=True,
)
async def subscription_events_count_daily(
    request: AuthHttpRequest, organization_slug: str
):
    org = await aget_object_or_404(
        Organization,
        slug=organization_slug,
        users=request.auth.user_id,
    )

    period = await get_current_period_dates(org)
    if period is None:
        return {"data": []}
    cycle_start, cycle_end = period

    today = timezone.now().date()
    period_start_date = cycle_start.date()
    period_end_date = min(cycle_end.date(), today)

    date_filter = Q(date__gte=cycle_start, date__lt=cycle_end)

    async def collect(queryset):
        return [row async for row in queryset.aiterator()]

    issue_rows, txn_rows, uptime_rows, log_rows = await asyncio.gather(
        collect(
            IssueEventProjectHourlyStatistic.objects.filter(
                Q(organization_id=org.id) & date_filter
            )
            .annotate(day=TruncDate("date"))
            .values("day")
            .annotate(total=Coalesce(Sum("count"), 0))
            .order_by("day")
        ),
        collect(
            TransactionEventProjectHourlyStatistic.objects.filter(
                Q(organization_id=org.id) & date_filter
            )
            .annotate(day=TruncDate("date"))
            .values("day")
            .annotate(total=Coalesce(Sum("count"), 0))
            .order_by("day")
        ),
        collect(
            UptimeCheckHourlyStatistic.objects.filter(
                Q(organization_id=org.id) & date_filter
            )
            .annotate(day=TruncDate("date"))
            .values("day")
            .annotate(total=Coalesce(Sum("count"), 0))
            .order_by("day")
        ),
        collect(
            LogProjectHourlyStatistic.objects.filter(
                Q(organization_id=org.id) & date_filter
            )
            .annotate(day=TruncDate("date"))
            .values("day")
            .annotate(total=Coalesce(Sum("count"), 0))
            .order_by("day")
        ),
    )
    issue_daily = {row["day"]: row["total"] for row in issue_rows}
    txn_daily = {row["day"]: row["total"] for row in txn_rows}
    uptime_daily = {row["day"]: row["total"] for row in uptime_rows}
    log_daily = {row["day"]: row["total"] for row in log_rows}

    # One entry per day, filling gaps with zeros. Per-day values are unfloored
    # billed contributions, so the daily series sums to the period total.
    data = []
    current = period_start_date
    while current <= period_end_date:
        day_counts = EventCounts(
            issue_event_count=issue_daily.get(current, 0),
            transaction_count=txn_daily.get(current, 0),
            uptime_check_event_count=uptime_daily.get(current, 0),
            log_count=log_daily.get(current, 0),
        )
        data.append(
            {
                "date": current,
                "event_count": day_counts.issue_event_count,
                "transaction_event_count": day_counts.transaction_count,
                "uptime_check_event_count": day_counts.billed(
                    "uptime_check_event_count"
                ),
                "log_event_count": day_counts.billed("log_count"),
            }
        )
        current += timedelta(days=1)

    return {"data": data}

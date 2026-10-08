import asyncio
import logging
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Prefetch, Q, QuerySet, Sum
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
from glitchtip.api.permissions import OrgScopedHttpRequest, org_scoped
from glitchtip.schema import CamelSchema

from .client import (
    add_subscription_item,
    create_customer,
    create_portal_session,
    create_session,
    create_subscription,
    fetch_subscription,
    list_customer_subscriptions,
)
from .constants import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    CollectionMethod,
    SubscriptionStatus,
)
from .exceptions import StripeError
from .models import StripePrice, StripeProduct, StripeSubscription
from .overage import (
    billable_units,
    cost_cents_for_units,
    cycle_quota,
    overage_active,
    overage_eligible,
    units_for_budget,
)
from .utils import (
    billing_window,
    compute_cycle,
    metered_item_period,
    select_subscription_items,
    unix_to_datetime,
)

logger = logging.getLogger(__name__)

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
    # The column is nullable (the overage product has no quota), but this schema
    # renders a subscription's plan, where a quota always exists. Keep the API
    # contract non-null so clients need no null branch, and coerce rather than
    # declare `int`: a bare annotation would 500 the whole subscription endpoint
    # if a quota-less product ever reached it.
    events: int

    class Meta:
        model = StripeProduct
        fields = ["name", "description", "events", "default_price"]

    @staticmethod
    def resolve_events(obj: StripeProduct) -> int:
        return obj.events or 0


class StripeProductExpandedPriceSchema(StripeIDSchema, ModelSchema):
    default_price: StripeNestedPriceSchema
    prices: list[StripeNestedPriceSchema]
    marketing_features: list[str]
    # Non-null by construction here: list_stripe_products filters events__gt=0,
    # which excludes NULL.
    events: int

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

    # The window usage is counted and billed in, which the metered item's own
    # period decides once one is attached (see billing_window).
    @staticmethod
    def resolve_subscription_cycle_start(obj: StripeSubscription):
        return billing_window(obj)[0]

    @staticmethod
    def resolve_subscription_cycle_end(obj: StripeSubscription):
        return billing_window(obj)[1]


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
    # Archived plans and archived prices are excluded: a retired price still
    # exists locally for the subscriptions grandfathered onto it, but must never
    # be offered again. is_public is a separate axis — a current price that
    # isn't publicly listed (non-profit, grandfathered) is simply not shown here.
    products = (
        StripeProduct.objects.filter(is_public=True, active=True, events__gt=0)
        .select_related("default_price")
        .prefetch_related(
            Prefetch(
                "stripeprice_set",
                queryset=StripePrice.objects.filter(is_public=True, active=True),
                to_attr="prices_list",
            )
        )
    )
    result = []
    async for product in products:
        result.append(product)
    return result


@router.get(
    "subscriptions/{slug:organization_id_or_slug}/",
    response=StripeSubscriptionSchema | None,
    by_alias=True,
)
@org_scoped(["org:read"], StripeSubscription)
async def get_stripe_subscription(
    request: OrgScopedHttpRequest,
    qs: QuerySet[StripeSubscription],
    organization_id_or_slug: str,
):
    return await (
        qs.filter(status__in=ACTIVE_SUBSCRIPTION_STATUSES)
        .select_related("price__product")
        .order_by("-created")
        .afirst()
    )


@router.post(
    "organizations/{slug:organization_id_or_slug}/create-stripe-subscription-checkout/",
    response=StripeCheckoutSessionSchema,
)
@org_scoped(["org:admin"], Organization)
async def create_stripe_session(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: PriceIDSchema,
):
    """
    Create Stripe Checkout, send to client for redirecting to Stripe
    See https://stripe.com/docs/api/checkout/sessions/create
    """
    organization = await aget_object_or_404(
        qs.select_related("owner__organization_user__user")
    )
    # Checkout creates a new subscription, so a paid org buying through it would
    # pay two base plans; it changes plan in the billing portal instead. A free
    # plan is a $0 subscription and costs nothing to leave behind.
    if await StripeSubscription.objects.filter(
        organization=organization,
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        price__price__gt=0,
    ).aexists():
        return JsonResponse(
            {"detail": "Organization already has a paid subscription"}, status=400
        )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    # Ensure the price exists, is current, and is a base plan; the metered
    # overage price is attached via configure_overage, never sold through
    # checkout. Archived prices 404 here rather than failing inside Stripe.
    price_id = payload.price
    await aget_object_or_404(
        StripePrice, stripe_id=price_id, is_metered=False, active=True
    )
    # The return URLs are frontend routes, which take the slug whichever form
    # the path used.
    return await create_session(price_id, customer_id, organization.slug)


@router.post(
    "organizations/{slug:organization_id_or_slug}/create-billing-portal/",
    response=StripePortalSessionSchema,
)
@org_scoped(["org:admin"], Organization)
async def stripe_billing_portal_session(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    """See https://stripe.com/docs/billing/subscriptions/integrating-self-serve-portal"""
    organization = await aget_object_or_404(
        qs.select_related("owner__organization_user__user")
    )
    if organization.stripe_customer_id:
        customer_id = organization.stripe_customer_id
    else:
        customer = await create_customer(organization)
        customer_id = customer.id
    return await create_portal_session(customer_id, organization.slug)


@router.post("subscriptions/", response=CreateSubscriptionResponse, by_alias=True)
async def stripe_create_subscription(request: AuthHttpRequest, payload: SubscriptionIn):
    org_id = int(payload.organization)
    organization = await aget_object_or_404(
        Organization.objects.select_related("owner__organization_user__user"),
        id=org_id,
        organization_users__role=OrganizationUserRole.OWNER,
        organization_users__user=request.auth.user_id,
    )
    # is_metered excludes the overage price, which also stores price=0 (its
    # cost lives in tiers) but is never a valid base plan.
    price = await aget_object_or_404(
        StripePrice.objects.select_related("product"),
        stripe_id=payload.price,
        price=0,
        is_metered=False,
        active=True,
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
    "subscriptions/{slug:organization_id_or_slug}/events_count/period/",
    response=SubscriptionUsageSchema,
    by_alias=True,
)
@org_scoped(["org:read"], Organization)
async def subscription_events_count_for_period(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
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

    org = await aget_object_or_404(qs)

    period = await get_current_period_dates(org, periods_ago)
    if period is None:
        # periods_ago precedes an annual subscription's start.
        return usage_response(EventCounts())

    start, end = period
    counts = await get_event_counts(org.id, start, end)
    return usage_response(counts)


@router.get(
    "subscriptions/{slug:organization_id_or_slug}/events_count/daily/",
    response=DailyEventsCountSchema,
    by_alias=True,
)
@org_scoped(["org:read"], Organization)
async def subscription_events_count_daily(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    org = await aget_object_or_404(qs)

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

    # NOTE: these four aggregates share one async connection per alias, so the
    # gather does not run them in parallel at the DB — same-alias async ORM
    # calls serialize over the single connection. The gather is kept for tidy
    # concurrent-style collection, not for query parallelism; true parallelism
    # would need independent connections, not worth it for this billing path.
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


# --- Metered (overage) billing ---------------------------------------------


class OverageStatusSchema(CamelSchema):
    """Current overage state for an org, for rendering the choice at the limit."""

    enabled: bool
    eligible: bool  # has an active paid subscription an overage item can attach to
    configured: bool  # an overage product/price is provisioned in Stripe
    active: bool  # what the throttle task actually enforces
    cap_cents: int
    # The ceiling configure_overage enforces; served so the client validates
    # against it rather than a copy that drifts or blocks a raised env var.
    max_cap_cents: int
    cap_units: int
    quota: int
    usage: int
    overage_units: int
    overage_cost_cents: int
    throttle_rate: int


class OverageConfigIn(CamelSchema):
    enabled: bool
    cap_cents: int = 0


async def _get_overage_price() -> StripePrice | None:
    """The live metered overage price, or None if the account has none.

    This lookup is the feature gate: overage billing is offered exactly when an
    overage product is live in the connected Stripe account. Archiving either
    the product or the price turns it off, and the product webhook applies that
    immediately — so `active` has to be honored on both sides of the join.

    Exactly one live overage price is expected. Should a second be provisioned
    (e.g. a re-priced tier schedule published before the old one is archived),
    the newest by Stripe id wins so behavior stays deterministic rather than
    depending on row order; archive the superseded price to resolve it.
    """
    return await (
        StripePrice.objects.filter(
            is_metered=True, active=True, product__is_overage=True, product__active=True
        )
        .select_related("product")
        .order_by("-stripe_id")
        .afirst()
    )


async def overage_status(org: Organization) -> dict:
    sub = org.stripe_primary_subscription
    # Same test configure_overage applies, so the card can't offer a toggle the
    # POST would reject.
    plan_events = (sub.price.product.events or 0) if sub else 0
    eligible = overage_eligible(sub)
    # Gate anything describing money on what the task enforces, not on the
    # sticky opt-in flag.
    active = overage_active(org)
    # Quota is a separate question from eligibility: an ineligible plan (unpaid,
    # comped, quota-less) still has whatever quota it has, and _check_and_update_
    # throttle enforces exactly this. Deriving it from `eligible` made the card
    # report the free-tier number while a larger one was actually being enforced.
    quota = plan_events or settings.GLITCHTIP_FREE_TIER_EVENTS

    usage = 0
    cap_units = units_for_budget(org.overage_spend_cap_cents)
    billed_units = 0
    period = await get_current_period_dates(org)
    if period:
        usage = (await get_event_counts(org.id, *period)).total_event_count
        # The same quota and cap the sweep bills against, so the card can't
        # show a smaller charge than the invoice after a downgrade or a lowered
        # cap.
        quota = cycle_quota(org, period[0], quota)
    overage_units = max(0, usage - quota)
    if period and active:
        billed_units = billable_units(
            overage_units,
            cap_units,
            org.overage_last_reported_units,
            org.overage_last_reported_at,
            *period,
        )

    return {
        "enabled": org.metered_billing_enabled,
        "eligible": eligible,
        "configured": await _get_overage_price() is not None,
        "active": active,
        "cap_cents": org.overage_spend_cap_cents,
        "max_cap_cents": settings.GLITCHTIP_OVERAGE_MAX_CAP_CENTS,
        "cap_units": cap_units,
        "quota": quota,
        "usage": usage,
        "overage_units": overage_units,
        "overage_cost_cents": cost_cents_for_units(billed_units),
        "throttle_rate": org.event_throttle_rate,
    }


@router.get(
    "subscriptions/{slug:organization_id_or_slug}/overage/",
    response=OverageStatusSchema,
    by_alias=True,
)
@org_scoped(["org:read"], Organization)
async def get_overage_status(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
):
    return await overage_status(
        await aget_object_or_404(
            qs.select_related("stripe_primary_subscription__price__product")
        )
    )


@router.post(
    "organizations/{slug:organization_id_or_slug}/overage/",
    response=OverageStatusSchema,
    by_alias=True,
)
@org_scoped(["org:admin"], Organization)
async def configure_overage(
    request: OrgScopedHttpRequest,
    qs: QuerySet[Organization],
    organization_id_or_slug: str,
    payload: OverageConfigIn,
):
    """Enable/disable metered overage billing and set the spend cap (owner-only).

    Enabling attaches the metered overage price as a second subscription item,
    whose own Stripe billing period becomes the organization's usage window
    from then on (see ``billing_window``). Disabling only clears the flag: the
    item stays attached (dormant, billing zero), so a re-enable within the
    same cycle can't re-bill reported usage and toggling never moves the
    window. Either way a throttle re-check is enqueued so the new headroom (or
    block) takes effect promptly.
    """
    org = await aget_object_or_404(
        qs.select_related("stripe_primary_subscription__price__product")
    )
    sub = org.stripe_primary_subscription

    if payload.enabled:
        if not overage_eligible(sub):
            return JsonResponse(
                {"detail": "An active paid plan is required for overage billing."},
                status=400,
            )
        if payload.cap_cents <= 0:
            return JsonResponse(
                {"detail": "A positive spend cap is required to enable overage."},
                status=400,
            )
        if payload.cap_cents > settings.GLITCHTIP_OVERAGE_MAX_CAP_CENTS:
            return JsonResponse(
                {
                    "detail": "Spend cap exceeds the maximum of "
                    f"{settings.GLITCHTIP_OVERAGE_MAX_CAP_CENTS} cents."
                },
                status=400,
            )
        overage_price = await _get_overage_price()
        if overage_price is None:
            return JsonResponse(
                {"detail": "Overage billing is not configured on this server."},
                status=400,
            )
        if not org.stripe_customer_id:
            # Nothing to attach to or meter; also keeps the customer-scoped
            # listing below from ever running unscoped.
            return JsonResponse(
                {"detail": "A billing account is required for overage billing."},
                status=400,
            )
        # A cap decrease on an already-enabled org only reduces exposure; it
        # must never be refused, whatever state the account's subscriptions are
        # in. Only while the primary subscription already carries the item,
        # though: with none attached the request below would attach one, and
        # that is exactly what the check guards.
        lowering_cap = (
            org.metered_billing_enabled
            and bool(sub.metered_item_id)
            and payload.cap_cents <= org.overage_spend_cap_cents
        )
        try:
            # The meter aggregates per customer, so two subscriptions each
            # carrying the metered price both bill the same usage. One
            # subscription per customer is the rule elsewhere; this is the
            # backstop for when it has already been broken.
            others = (
                []
                if lowering_cap
                else await list_customer_subscriptions(org.stripe_customer_id)
            )
            for other in others:
                if other.id == sub.stripe_id:
                    continue
                if select_subscription_items(other.items)[1] is not None:
                    logger.warning(
                        "Refusing overage for org %s: subscription %s also carries "
                        "the metered price",
                        org.id,
                        other.id,
                    )
                    return JsonResponse(
                        {
                            "detail": "Another subscription on this account already "
                            "has overage billing. Contact support to consolidate "
                            "your subscriptions first."
                        },
                        status=400,
                    )
            if not sub.metered_item_id:
                # Stripe is the source of truth: a crash between attaching the
                # item and saving its id here leaves an orphan that Stripe would
                # reject as a duplicate on retry, so reuse it instead of re-adding.
                fetched = await fetch_subscription(sub.stripe_id)
                _, item = select_subscription_items(fetched.items)
                if item is None:
                    item = await add_subscription_item(
                        sub.stripe_id, overage_price.stripe_id
                    )
                sub.metered_item_id = item.id
                # Stripe owns the item's period. Record it now rather than wait
                # for the webhook: the throttle re-check enqueued below bills
                # against it.
                sub.metered_period_start, sub.metered_period_end = metered_item_period(
                    item
                )
                await sub.asave(
                    update_fields=[
                        "metered_item_id",
                        "metered_period_start",
                        "metered_period_end",
                    ]
                )
        except StripeError as e:
            # Card declines are safe to show the owner; other Stripe errors
            # may leak internals, so log those and return a generic message.
            if e.type == "card_error":
                return JsonResponse({"detail": e.message}, status=402)
            logger.exception(
                "Overage enable failed for org %s (status=%s type=%s code=%s)",
                org.id,
                e.status,
                e.type,
                e.code,
            )
            return JsonResponse(
                {"detail": "Could not enable overage billing. Please try again."},
                status=502,
            )
        org.metered_billing_enabled = True
        org.overage_spend_cap_cents = payload.cap_cents
        await org.asave(
            update_fields=["metered_billing_enabled", "overage_spend_cap_cents"]
        )
    else:
        # Keep the item attached and the last-report record intact: a re-enable
        # is then a flag flip, and the cap stays floored at what the cycle has
        # billed so toggling can't retract it. A dormant item bills zero.
        org.metered_billing_enabled = False
        await org.asave(update_fields=["metered_billing_enabled"])

    await check_organization_throttle.aenqueue(org.id, bypass_cache=True)
    return await overage_status(
        await Organization.objects.select_related(
            "stripe_primary_subscription__price__product"
        ).aget(id=org.id)
    )

import logging
from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

# Safety cap for the monthly-cycle advancement loop.  120 months (10 years)
# is far beyond any realistic catch-up window but prevents a runaway loop
# if period_start is corrupt or absurdly old.
_MAX_CYCLE_MONTHS = 120


def get_stripe_link(stripe_id: str) -> str:
    base = "https://dashboard.stripe.com"
    path = "/"
    key = settings.STRIPE_SECRET_KEY
    if key and key.startswith("sk_test"):
        path += "test/"
    if stripe_id.startswith("sub"):
        path += "subscriptions/"
    if stripe_id.startswith("prod"):
        path += "products/"
    if stripe_id.startswith("cus"):
        path += "customers/"
    return f"{base}{path}{stripe_id}"


def is_metered_price(price) -> bool:
    """True if a Stripe Price schema object is usage-based (meter-backed)."""
    recurring = getattr(price, "recurring", None) or {}
    return recurring.get("usage_type") == "metered" or bool(recurring.get("meter"))


def price_kwargs(price) -> dict:
    """Map a Stripe Price (schema object) to StripePrice field values.

    Shared by both sync paths. Tiered/metered prices have ``unit_amount=None``
    (the per-unit cost lives in tiers, not on the price), so ``price`` is stored
    as 0 and the graduated schedule in settings is the source of truth for cost.
    """
    meta = price.metadata or {}
    recurring = price.recurring or {}
    return {
        "active": price.active,
        "price": price.unit_amount / 100 if price.unit_amount is not None else 0,
        "nickname": price.nickname or "",
        "no_throttle": meta.get("no_throttle", "").lower() == "true",
        "is_public": meta.get("is_public", "").lower() == "true",
        "is_metered": is_metered_price(price),
        "meter_id": recurring.get("meter") or "",
        "interval": recurring.get("interval", "month"),
    }


def is_syncable_product(product) -> bool:
    """True for products GlitchTip stores locally.

    Hosted plans carry an ``events`` quota; the metered overage product
    (product_type=overage) has no quota but must be synced so its tiered price
    can be attached as a second subscription item. Everything else in the
    account — notably self-host support licenses — is deliberately not stored.
    """
    product_type = product.metadata.get("product_type", "").lower()
    if product_type == "overage":
        return True
    return product_type == "hosted" and "events" in product.metadata


def product_kwargs(product) -> dict:
    """Map a Stripe Product (schema object) to StripeProduct field values.

    Shared by the periodic sync and the product webhook so the two can't drift.
    """
    metadata = product.metadata
    is_overage = metadata.get("product_type", "").lower() == "overage"
    return {
        "name": product.name,
        "description": product.description or "",
        "active": product.active,
        # The overage product has no quota. NULL says "quota is not a concept
        # here", which 0 ("a plan that allows no events") cannot express.
        "events": None if is_overage else int(metadata.get("events", 0)),
        # Optional: absent metadata leaves the plan silent so the instance
        # default applies. A malformed value raises, as a bad `events` does,
        # rather than silently becoming an unintended limit.
        "uptime_monitors": (
            None
            if not metadata.get("uptime_monitors")
            else int(metadata["uptime_monitors"])
        ),
        "is_public": metadata.get("is_public", "").lower() == "true",
        "is_overage": is_overage,
        "allow_sso": metadata.get("sso", "").lower() == "true",
        "marketing_features": [
            f["name"] for f in product.marketing_features if f.get("name")
        ],
    }


def select_subscription_items(items):
    """Split a subscription's items into (base_item, metered_item).

    The base licensed plan drives quota and billing-cycle dates; the metered
    item (if present) is the overage price. Stripe doesn't guarantee item order,
    so callers must not assume ``items.data[0]`` is the base plan.
    """
    base = metered = None
    for item in items.data:
        if is_metered_price(item.price):
            metered = metered or item
        else:
            base = base or item
    if base is None and items.data:
        base = items.data[0]
    return base, metered


def unix_to_datetime(timestamp: int) -> datetime:
    """Convert a POSIX timestamp from Stripe into an aware UTC datetime.

    Uses an explicit UTC tzinfo so the result does not depend on the
    process's local timezone (``$TZ``).
    """
    return datetime.fromtimestamp(timestamp, tz=UTC)


def metered_item_period(metered_item) -> tuple[datetime | None, datetime | None]:
    """The metered item's current Stripe billing period; (None, None) without one."""
    if metered_item is None:
        return None, None
    return (
        unix_to_datetime(metered_item.current_period_start),
        unix_to_datetime(metered_item.current_period_end),
    )


def has_metered_window(sub) -> bool:
    """Whether ``sub`` carries a metered item whose Stripe period is recorded."""
    return bool(
        sub.metered_item_id and sub.metered_period_start and sub.metered_period_end
    )


def billing_window(sub) -> tuple[datetime, datetime]:
    """The window ``sub``'s usage is counted, throttled and billed in.

    Stripe files a meter event under the billing period of the metered item
    that contains the event's timestamp (its documented behaviour for
    item-level periods; the `last` pick by timestamp within a period was
    verified in test mode), so while an item is attached its own period is
    the window -- whatever Stripe holds for it, whether or not that lines up
    with the base plan's period or the virtual month. Counting over
    any other window would bill twice where the two diverge: a cycle-to-date
    total counted over a window that outlives the item's period lands in the
    next one as if it were that period's own. The item's period applies
    whether or not overage is currently switched on, so toggling it never
    moves the window (and so never hands out a fresh quota). Without an item,
    or before its period has been recorded, the base plan's Stripe period is
    the window, subdivided into virtual months for annual plans.

    In Stripe test mode an item attached part-way through a month to an
    annual subscription was given the anchor-aligned month as its first
    period, so the two windows were observed to coincide; nothing here depends
    on that.
    """
    if has_metered_window(sub):
        return sub.metered_period_start, sub.metered_period_end
    return (
        sub.subscription_cycle_start or sub.current_period_start,
        sub.subscription_cycle_end or sub.current_period_end,
    )


def compute_cycle(period_start: datetime, period_end: datetime, is_annual: bool):
    """Return (cycle_start, cycle_end) for a subscription period.

    Monthly plans use the full Stripe billing period.  Annual plans are
    subdivided into virtual monthly cycles for quota counting — this
    function advances from period_start until the cycle covers now.
    """
    if not is_annual:
        return period_start, period_end

    now = timezone.now()
    month = 0
    cycle_start = period_start
    cycle_end = period_start + relativedelta(months=1)
    while cycle_end < now and month < _MAX_CYCLE_MONTHS:
        month += 1
        cycle_start = period_start + relativedelta(months=month)
        cycle_end = period_start + relativedelta(months=month + 1)
    if month >= _MAX_CYCLE_MONTHS:
        logger.warning(
            "compute_cycle hit %d-month cap; period_start=%s is likely stale",
            _MAX_CYCLE_MONTHS,
            period_start,
        )
    return cycle_start, cycle_end


def compute_cycle_n_ago(
    current_period_start: datetime,
    current_period_end: datetime,
    subscription_cycle_start: datetime | None,
    subscription_cycle_end: datetime | None,
    periods_ago: int,
) -> tuple[datetime, datetime] | None:
    """Return (start, end) for a billing cycle N periods before the current one.

    periods_ago=0 is not handled here (use with_event_counts current_period=True).
    periods_ago=1 returns the immediately preceding cycle.

    Returns None if the requested period precedes the subscription start
    (annual plans only — monthly plans always have a valid prior period).

    Monthly plans: go back periods_ago months from current_period_start.
    Annual plans with virtual monthly cycles: go back periods_ago months from
    subscription_cycle_start, but only if the result doesn't precede period_start.
    """
    if subscription_cycle_start and subscription_cycle_end:
        # Annual plan with virtual monthly cycles
        cycle_start = subscription_cycle_start - relativedelta(months=periods_ago)
        cycle_end = subscription_cycle_start - relativedelta(months=periods_ago - 1)
        if cycle_start < current_period_start:
            return None
        return cycle_start, cycle_end
    else:
        # Monthly plan
        period_end = current_period_start - relativedelta(months=periods_ago - 1)
        period_start = current_period_start - relativedelta(months=periods_ago)
        return period_start, period_end

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone

from .constants import ACTIVE_SUBSCRIPTION_STATUSES
from .models import StripePrice, StripeProduct, StripeSubscription


async def sync_stripe_models():
    if settings.BILLING_ENABLED:
        await StripeProduct.sync_from_stripe()
        await StripePrice.sync_from_stripe()
        await StripeSubscription.sync_from_stripe()


async def update_subscription_cycles():
    """
    Roll forward virtual monthly cycles for annual plans.

    Annual Stripe subscriptions use a single billing period but GlitchTip
    subdivides it into monthly cycles for quota counting. This advances
    stale cycles so throttle checks use the correct window.
    """
    if not settings.BILLING_ENABLED:
        return

    now = timezone.now()
    qs = StripeSubscription.objects.filter(
        status__in=ACTIVE_SUBSCRIPTION_STATUSES,
        subscription_cycle_end__lt=now,
        price__interval="year",
    ).select_related("price")

    async for sub in qs.aiterator():
        while sub.subscription_cycle_end < now:
            sub.subscription_cycle_start = sub.subscription_cycle_end
            sub.subscription_cycle_end = sub.subscription_cycle_start + relativedelta(
                months=1
            )

        await sub.asave(
            update_fields=["subscription_cycle_start", "subscription_cycle_end"]
        )

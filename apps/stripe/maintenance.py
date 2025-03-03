from django.conf import settings

from .models import StripePrice, StripeProduct, StripeSubscription


async def sync_stripe_models():
    if settings.BILLING_ENABLED:
        await StripeProduct.sync_from_stripe()
        await StripePrice.sync_from_stripe()
        await StripeSubscription.sync_from_stripe()

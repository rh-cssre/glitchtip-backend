import hmac
import logging
import time

import aiohttp
from django.conf import settings
from django.core.cache import cache
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseServerError,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from pydantic import ValidationError

from apps.organizations_ext.models import Organization
from apps.organizations_ext.tasks import check_organization_throttle
from glitchtip.utils import async_call_celery_task

from .client import stripe_get
from .constants import ACTIVE_SUBSCRIPTION_STATUSES
from .models import StripePrice, StripeProduct, StripeSubscription
from .schema import Customer, Price, Product, StripeEvent, Subscription
from .utils import unix_to_datetime

logger = logging.getLogger(__name__)


async def update_product(product: Product):
    metadata = product.metadata
    if "events" not in metadata:
        return

    await StripeProduct.objects.aupdate_or_create(
        stripe_id=product.id,
        defaults={
            "name": product.name,
            "description": product.description,
            "events": metadata["events"],
            "is_public": metadata.get("is_public") == "true",
        },
    )


async def update_price(price: Price):
    if (
        not price.unit_amount
        or not await StripeProduct.objects.filter(stripe_id=price.product).aexists()
    ):
        return

    await StripePrice.objects.aupdate_or_create(
        stripe_id=price.id,
        defaults={
            "product_id": price.product,
            "nickname": price.nickname or "",
            "price": price.unit_amount / 100,
        },
    )


async def update_subscription(subscription: Subscription, request: HttpRequest):
    customer_obj = Customer.model_validate_json(
        await stripe_get(f"customers/{subscription.customer}")
    )
    customer_metadata = customer_obj.metadata
    if not customer_metadata:
        logger.warning(f"Customer {customer_obj.id} has no metadata")
        return
    try:
        organization_id = int(
            customer_metadata.get(
                "organization_id", customer_metadata.get("djstripe_subscriber")
            )
        )
    except TypeError:
        logger.warning(
            f"Customer {customer_obj.id} has no organization_id", exc_info=True
        )
        return
    if not organization_id:
        return

    # Check region, is it this region or should it be forwarded
    region = customer_metadata.get("region", "")
    if region != settings.STRIPE_REGION:
        if from_region := request.headers.get("From-Region"):
            logger.warning(
                f"Received webhook from region {from_region} but server is region {settings.STRIPE_REGION}"
            )
            return
        if forward_region := settings.STRIPE_REGION_DOMAINS.get(region):
            forward_url = forward_region + request.path
            headers = {
                "Stripe-Signature": request.headers.get("Stripe-Signature"),
                "Content-Type": "application/json",
                "From-Region": settings.STRIPE_REGION,
            }
            async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
                async with session.post(
                    forward_url, data=request.body, headers=headers, ssl=True
                ) as response:
                    await response.read()
        return

    organization = await Organization.objects.filter(id=organization_id).afirst()
    if not organization:
        return

    if (price_id := subscription.items.data[0].price.id) is None:
        return
    stripe_subscription, created = await StripeSubscription.objects.aupdate_or_create(
        stripe_id=subscription.id,
        defaults={
            "created": unix_to_datetime(subscription.created),
            "current_period_start": unix_to_datetime(
                subscription.items.data[0].current_period_start
            ),
            "current_period_end": unix_to_datetime(
                subscription.items.data[0].current_period_end
            ),
            "price_id": price_id,
            "organization_id": organization.id,
            "status": subscription.status,
            "start_date": unix_to_datetime(subscription.start_date),
            "collection_method": subscription.collection_method,
        },
    )
    if stripe_subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
        primary_subscription = await StripeSubscription.get_primary_subscription(
            organization
        )
        if (
            primary_subscription
            and primary_subscription.stripe_id
            != organization.stripe_primary_subscription_id
        ):
            organization.stripe_primary_subscription = primary_subscription
            await organization.asave(update_fields=["stripe_primary_subscription"])
        await async_call_celery_task(check_organization_throttle, organization.id, True)

    # Primary subscription should be removed if status is not active
    elif stripe_subscription.stripe_id is organization.stripe_primary_subscription_id:
        organization.stripe_primary_subscription = None
        await organization.asave(update_fields=["stripe_primary_subscription"])


@csrf_exempt
@require_POST
async def stripe_webhook_view(request: HttpRequest, event_type: str | None = None):
    """
    Handles Stripe webhook events.

    This view verifies the webhook signature using the raw request body and the
    Stripe webhook secret.  It then processes the event based on its type.
    """
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    if not sig_header:
        logger.warning("Stripe webhook received without signature header.")
        return HttpResponseForbidden("Missing signature header")

    try:
        if not verify_stripe_signature(payload, sig_header, event_type):
            logger.warning("Stripe webhook signature verification failed.")
            return HttpResponseForbidden("Invalid signature")
    except ValueError as e:
        logger.error(f"Error during signature verification: {e}")
        return HttpResponseForbidden("Invalid payload")
    except Exception as e:
        logger.exception(
            f"Unexpected error verifying signature: {e}"
        )  # Catch unexpected exceptions
        return HttpResponseServerError("Internal Server Error")

    try:
        event = StripeEvent.model_validate_json(payload)
    except ValidationError as e:
        logger.warning("Invalid JSON payload in Stripe webhook.", exc_info=e)
        return HttpResponse(status=200)

    last_event_for_object = cache.get_or_set(
        "stripe" + event.data.object.id, event.created, 600
    )
    if event.created < last_event_for_object:
        return HttpResponse(status=200)

    if not cache.add("stripe" + event.id, None, 600):
        return HttpResponse(status=200)

    if event.type in ["product.updated", "product.created"]:
        await update_product(event.data.object)
    elif event.type in [
        "customer.subscription.updated",
        "customer.subscription.created",
        "customer.subscription.deleted",
    ]:
        await update_subscription(event.data.object, request)
    elif event.type in ["price.updated", "price.created"]:
        await update_price(event.data.object)
    else:
        logger.info(f"Unhandled Stripe event type: {event.type}")

    return HttpResponse(status=200)


def verify_stripe_signature(payload, sig_header, event_type: str):
    """Verifies the Stripe webhook signature.

    Args:
        payload: The raw request body (bytes).
        sig_header: The value of the Stripe-Signature header.

    Returns:
        True if the signature is valid, False otherwise.
    Raises:
        ValueError: if the signature header is malformed.
    """

    webhook_secret = (
        settings.STRIPE_WEBHOOK_SECRET_SUBSCRIPTION
        if event_type == "subscription"
        else settings.STRIPE_WEBHOOK_SECRET
    )
    if not webhook_secret:
        logger.error("STRIPE_WEBHOOK_SECRET not configured in settings.")
        #  Return False/raise exception based on desired behavior (security vs. failing fast).
        #  Returning False is generally safer.
        return False

    try:
        parts = {}
        for part in sig_header.split(","):
            key, value = part.strip().split("=", 1)
            parts[key.strip()] = value.strip()

        timestamp = int(parts.get("t"))
        signature = parts.get("v1")  # Or 'v0' depending on your webhook setting

        if not timestamp or not signature:
            raise ValueError("Missing timestamp or signature")

        # Check timestamp tolerance (prevent replay attacks)
        tolerance = getattr(
            settings, "STRIPE_WEBHOOK_TOLERANCE", 300
        )  # Default: 5 minutes
        if (time.time() - timestamp) > tolerance:
            logger.warning(
                f"Stripe Webhook timestamp outside of tolerance: {timestamp}"
            )
            return False

        # Construct the signed payload string.
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"

        # Compute the expected signature.
        expected_signature = hmac.new(
            webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        # Compare signatures. Use hmac.compare_digest for constant-time comparison.
        return hmac.compare_digest(signature, expected_signature)

    except ValueError:
        raise
    except Exception as e:
        logger.exception(f"Error in verify_stripe_signature: {e}")
        return False

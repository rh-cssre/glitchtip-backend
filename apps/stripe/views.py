import hmac
import logging
import time

from django.conf import settings
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

from .client import stripe_get
from .models import StripeProduct, StripeSubscription
from .schema import Customer, Price, Product, StripeEvent, Subscription
from .utils import unix_to_datetime

logger = logging.getLogger(__name__)


async def update_product(product: Product):
    metadata = product.metadata
    if "events" not in metadata:
        return

    # Get price
    price_obj = Price.model_validate_json(
        await stripe_get(f"prices/{product.default_price}")
    )
    price = price_obj.unit_amount / 100

    await StripeProduct.objects.aupdate_or_create(
        stripe_id=product.id,
        defaults={
            "name": product.name,
            "description": product.description,
            "price": price,
            "events": metadata["events"],
            "is_public": metadata.get("is_public") == "true",
        },
    )


async def update_subscription(subscription: Subscription):
    customer_obj = Customer.model_validate_json(
        await stripe_get(f"customers/{subscription.customer}")
    )
    customer_metadata = customer_obj.metadata
    organization_id = int(
        customer_metadata.get(
            "organization_id", customer_metadata["djstripe_subscriber"]
        )
    )
    if not organization_id:
        return
    organization = await Organization.objects.filter(id=organization_id).afirst()
    if not organization:
        return

    if (
        product_id := subscription.items.data
        and subscription.items.data[0].get("plan", {}).get("product")
    ) is None:
        return
    await StripeSubscription.objects.aupdate_or_create(
        stripe_id=subscription.id,
        defaults={
            "created": unix_to_datetime(subscription.created),
            "current_period_start": unix_to_datetime(subscription.current_period_start),
            "current_period_end": unix_to_datetime(subscription.current_period_end),
            "product_id": product_id,
            "organization_id": organization.id,
            "is_active": True,
        },
    )


@csrf_exempt
@require_POST
async def stripe_webhook_view(request: HttpRequest):
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
        if not verify_stripe_signature(payload, sig_header):
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
    except ValidationError:
        logger.warning("Invalid JSON payload in Stripe webhook.")
        return HttpResponse(status=200)

    if event.type in ["product.updated", "product.created"]:
        await update_product(event.data.object)
    elif event.type in [
        "customer.subscription.updated",
        "customer.subscription.created",
    ]:
        await update_subscription(event.data.object)
    else:
        logger.info(f"Unhandled Stripe event type: {event.type}")

    return HttpResponse(status=200)


def verify_stripe_signature(payload, sig_header):
    """Verifies the Stripe webhook signature.

    Args:
        payload: The raw request body (bytes).
        sig_header: The value of the Stripe-Signature header.

    Returns:
        True if the signature is valid, False otherwise.
    Raises:
        ValueError: if the signature header is malformed.
    """

    webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
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
        signed_payload = (
            f"{timestamp}.{payload.decode('utf-8')}"  # Important: decode to string
        )

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

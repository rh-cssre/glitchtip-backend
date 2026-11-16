import hmac
import json
import time

from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.schema import (
    EventData,
    Price,
    StripeEvent,
    StripeEventRequest,
    Subscription,
    SubscriptionItem,
    SubscriptionItems,
)


class StripeWebhookMixin:
    """Signed subscription-webhook requests, shared by the webhook test cases."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.factory = RequestFactory()
        self.url = reverse("stripe_webhook")
        self.webhook_secret = "test_webhook_secret"  # Use a test secret

    def generate_stripe_request(self, payload):
        payload_bytes = json.dumps(payload).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}
        return self.factory.post(
            self.url, data=payload_bytes, content_type="application/json", **headers
        )

    def generate_subscription_event_data(
        self,
        type,
        event_id,
        subscription_id,
        current_period_start,
        current_period_end,
        price_id,
        product_id,
        status=SubscriptionStatus.ACTIVE,
        event_created_timestamp=None,
        metered_item_id=None,
        metered_period=None,
    ):
        # A metered overage item, when the caller wants the subscription to look
        # like one that opted into overage billing. Its period is the plan's
        # unless the caller says otherwise: in Stripe each item has its own.
        metered_period = metered_period or (current_period_start, current_period_end)
        metered_items = (
            [
                SubscriptionItem(
                    id=metered_item_id,
                    object="subscription_item",
                    created=current_period_start,
                    current_period_end=metered_period[1],
                    current_period_start=metered_period[0],
                    metadata={},
                    price=Price(
                        object="price",
                        active=True,
                        billing_scheme="tiered",
                        created=0,
                        currency="usd",
                        livemode=False,
                        lookup_key=None,
                        nickname=None,
                        recurring={"usage_type": "metered", "meter": "mtr_1"},
                        tax_behavior=None,
                        tiers_mode="graduated",
                        type="",
                        unit_amount_decimal=None,
                        metadata={},
                        id="price_overage",
                        product="prod_overage",
                        unit_amount=None,
                    ),
                    quantity=1,
                    subscription=subscription_id,
                    tax_rates=[],
                )
            ]
            if metered_item_id
            else []
        )
        data = StripeEvent(
            type=type,
            id=event_id,
            data=EventData(
                object=Subscription(
                    object="subscription",
                    id=subscription_id,
                    customer="cus_test",
                    items=SubscriptionItems(
                        object="list",
                        data=[
                            SubscriptionItem(
                                id="test_subscription_item",
                                object="subscription_item",
                                created=current_period_start,
                                current_period_end=current_period_end,
                                current_period_start=current_period_start,
                                metadata={},
                                price=Price(
                                    object="price",
                                    active=True,
                                    billing_scheme=None,
                                    created=0,
                                    currency="",
                                    livemode=False,
                                    lookup_key=None,
                                    nickname=None,
                                    recurring=None,
                                    tax_behavior=None,
                                    tiers_mode=None,
                                    type="",
                                    unit_amount_decimal="1",
                                    metadata={},
                                    id=price_id,
                                    product=product_id,
                                    unit_amount=1,
                                ),
                                quantity=1,
                                subscription=subscription_id,
                                tax_rates=[],
                            )
                        ]
                        + metered_items,
                    ),
                    created=current_period_start,
                    status=status,
                    livemode=False,
                    metadata={},
                    cancel_at_period_end=False,
                    start_date=current_period_start,
                    collection_method="charge_automatically",
                )
            ),
            api_version="",
            created=event_created_timestamp or current_period_start,
            livemode=False,
            pending_webhooks=1,
            request=StripeEventRequest(id="req_test", idempotency_key="test_key"),
        )
        return data.model_dump()

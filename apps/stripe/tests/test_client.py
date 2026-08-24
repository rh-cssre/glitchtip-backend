import json
from unittest.mock import patch

from aioresponses import aioresponses
from django.test import TestCase

from apps.stripe.client import (
    STRIPE_URL,
    STRIPE_V2_URL,
    create_session,
    create_subscription,
    fetch_v2_event,
    migrate_subscription_to_flexible,
    stripe_get,
    stripe_post,
)
from apps.stripe.exceptions import StripeError, StripeResourceNotFound
from apps.stripe.schema import (
    AutomaticTax,
    CustomText,
    PhoneNumberCollection,
    Price,
    Session,
    Subscription,
    SubscriptionItem,
    SubscriptionItems,
    TotalDetails,
)


class StripeClientRetryTests(TestCase):
    def setUp(self):
        self.sleep_patcher = patch("apps.stripe.client.asyncio.sleep")
        self.mock_sleep = self.sleep_patcher.start()

        async def _no_sleep(_):
            return None

        self.mock_sleep.side_effect = _no_sleep
        self.addCleanup(self.sleep_patcher.stop)

    async def test_stripe_get_retries_on_429_should_retry(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(
                url,
                status=429,
                payload={"error": {"message": "lock_timeout"}},
                headers={"Stripe-Should-Retry": "true"},
            )
            mocked.get(url, status=200, body='{"ok": true}')
            body = await stripe_get("customers/cus_test")
        self.assertEqual(body, '{"ok": true}')

    async def test_stripe_get_retries_on_5xx_without_header(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(url, status=503, payload={"error": {"message": "unavailable"}})
            mocked.get(url, status=200, body='{"ok": true}')
            body = await stripe_get("customers/cus_test")
        self.assertEqual(body, '{"ok": true}')

    async def test_stripe_get_does_not_retry_when_header_says_false(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(
                url,
                status=429,
                payload={"error": {"message": "do not retry"}},
                headers={"Stripe-Should-Retry": "false"},
            )
            with self.assertRaises(StripeError) as ctx:
                await stripe_get("customers/cus_test")
        self.assertEqual(ctx.exception.status, 429)

    async def test_stripe_get_raises_after_exhausting_retries(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            for _ in range(4):
                mocked.get(
                    url,
                    status=429,
                    payload={"error": {"message": "still locked"}},
                    headers={"Stripe-Should-Retry": "true"},
                )
            with self.assertRaises(StripeError) as ctx:
                await stripe_get("customers/cus_test")
        self.assertEqual(ctx.exception.status, 429)

    async def test_stripe_get_raises_resource_not_found_without_retry(self):
        url = f"{STRIPE_URL}/customers/cus_test"
        with aioresponses() as mocked:
            mocked.get(url, status=404, payload={"error": {"message": "missing"}})
            with self.assertRaises(StripeResourceNotFound):
                await stripe_get("customers/cus_test")

    async def test_stripe_post_retries_on_429(self):
        url = f"{STRIPE_URL}/customers"
        with aioresponses() as mocked:
            mocked.post(
                url,
                status=429,
                payload={"error": {"message": "lock_timeout"}},
                headers={"Stripe-Should-Retry": "true"},
            )
            mocked.post(url, status=200, body=json.dumps({"id": "cus_new"}))
            body = await stripe_post("customers", {"name": "x"})
        self.assertEqual(json.loads(body)["id"], "cus_new")


class StripeV2EventFetchTests(TestCase):
    async def test_fetch_does_not_retry(self):
        # Runs inline in a webhook response; Stripe's redelivery is the retry
        # mechanism, so a transient failure must raise after one attempt.
        url = f"{STRIPE_V2_URL}/core/events/evt_1"
        with aioresponses() as mocked:
            mocked.get(
                url,
                status=500,
                payload={"error": {"message": "flake"}},
                headers={"Stripe-Should-Retry": "true"},
            )
            with self.assertRaises(StripeError) as ctx:
                await fetch_v2_event("evt_1")
        self.assertEqual(ctx.exception.status, 500)

    async def test_fetch_rejects_malformed_event_id(self):
        # The id lands in the URL path; a crafted one must not reach Stripe.
        with self.assertRaises(ValueError):
            await fetch_v2_event("evt_1/../../v1/customers")


def _session_response() -> str:
    """A minimal valid Checkout Session body for create_session to parse."""
    return Session(
        id="cs_test_1",
        object="checkout.session",
        amount_subtotal=0,
        amount_total=0,
        automatic_tax=AutomaticTax(enabled=True),
        created=0,
        currency="usd",
        custom_fields=[],
        custom_text=CustomText(),
        customer_creation=None,
        expires_at=0,
        invoice_creation=None,
        livemode=False,
        metadata={},
        mode="subscription",
        payment_method_collection="always",
        payment_method_options={},
        payment_method_types=["card"],
        payment_status="unpaid",
        phone_number_collection=PhoneNumberCollection(enabled=False),
        shipping_options=[],
        status="open",
        success_url="https://example.com/success",
        total_details=TotalDetails(amount_discount=0, amount_shipping=0, amount_tax=0),
        url="https://checkout.stripe.com/c/pay/cs_test_1",
    ).model_dump_json()


def _subscription_response() -> str:
    """A minimal valid Subscription body for create_subscription to parse."""
    price = Price(
        object="price",
        id="price_1",
        active=True,
        billing_scheme="per_unit",
        created=0,
        currency="usd",
        livemode=False,
        lookup_key=None,
        nickname=None,
        product="prod_1",
        recurring={"interval": "month"},
        tax_behavior=None,
        tiers_mode=None,
        type="recurring",
        unit_amount=0,
        unit_amount_decimal="0",
        metadata={},
    )
    return Subscription(
        object="subscription",
        id="sub_1",
        items=SubscriptionItems(
            object="list",
            data=[
                SubscriptionItem(
                    id="si_1",
                    object="subscription_item",
                    created=0,
                    current_period_end=0,
                    current_period_start=0,
                    metadata={},
                    price=price,
                    subscription="sub_1",
                    tax_rates=[],
                )
            ],
        ),
        created=0,
        status="active",
        livemode=False,
        metadata={},
        cancel_at_period_end=False,
        start_date=0,
        collection_method="charge_automatically",
        customer="cus_1",
    ).model_dump_json()


class StripeBillingModeTests(TestCase):
    """Subscriptions must be born in flexible billing mode.

    Stripe's default depends on the pinned Stripe-Version, and classic mode
    can't accept a metered price, so both creation paths state the mode
    outright. Asserting the wire payload is the only way to catch a regression
    here: a subscription created classic can be migrated but never reverted.
    """

    async def test_create_subscription_requests_flexible_mode(self):
        with aioresponses() as mocked:
            mocked.post(
                f"{STRIPE_URL}/subscriptions", status=200, body=_subscription_response()
            )
            await create_subscription("cus_1", "price_1")
            request = next(iter(mocked.requests.values()))[0]
        self.assertEqual(request.kwargs["data"]["billing_mode[type]"], "flexible")

    async def test_create_session_requests_flexible_mode(self):
        with aioresponses() as mocked:
            mocked.post(
                f"{STRIPE_URL}/checkout/sessions", status=200, body=_session_response()
            )
            await create_session("price_1", "cus_1", "my-org")
            request = next(iter(mocked.requests.values()))[0]
        self.assertEqual(
            request.kwargs["data"]["subscription_data[billing_mode][type]"], "flexible"
        )

    async def test_migrate_posts_flexible_mode(self):
        with aioresponses() as mocked:
            mocked.post(
                f"{STRIPE_URL}/subscriptions/sub_1/migrate", status=200, body="{}"
            )
            await migrate_subscription_to_flexible("sub_1")
            request = next(iter(mocked.requests.values()))[0]
        self.assertEqual(request.kwargs["data"]["billing_mode[type]"], "flexible")

import json
from unittest.mock import AsyncMock, patch

from django.core import mail
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.schema import (
    Price,
    Subscription,
    SubscriptionItem,
    SubscriptionItems,
)
from apps.stripe.views import update_subscription

SUPPORT_SUB_ID = "sub_support123"


def build_subscription(
    subscription_id=SUPPORT_SUB_ID,
    product_id="prod_support",
    status=SubscriptionStatus.ACTIVE,
    metadata=None,
):
    now = int(timezone.now().timestamp())
    return Subscription(
        object="subscription",
        id=subscription_id,
        customer="cus_test",
        items=SubscriptionItems(
            object="list",
            data=[
                SubscriptionItem(
                    id="si_test",
                    object="subscription_item",
                    created=now,
                    current_period_end=now + 2592000,
                    current_period_start=now,
                    metadata={},
                    price=Price(
                        object="price",
                        active=True,
                        billing_scheme=None,
                        created=0,
                        currency="usd",
                        livemode=False,
                        lookup_key=None,
                        nickname=None,
                        recurring=None,
                        tax_behavior=None,
                        tiers_mode=None,
                        type="",
                        unit_amount_decimal="1",
                        metadata={},
                        id="price_support",
                        product=product_id,
                        unit_amount=1,
                    ),
                    quantity=1,
                    subscription=subscription_id,
                    tax_rates=[],
                )
            ],
        ),
        created=now,
        status=status,
        livemode=False,
        metadata=metadata or {},
        cancel_at_period_end=False,
        start_date=now,
        collection_method="charge_automatically",
    )


def stripe_get_side_effect(product_type="support", email="buyer@example.com"):
    """Return customer JSON for customers/*, product JSON for products/*."""

    async def _side_effect(path):
        if path.startswith("customers/"):
            return json.dumps(
                {
                    "object": "customer",
                    "id": "cus_test",
                    "email": email,
                    "metadata": {},
                    "name": None,
                }
            )
        if path.startswith("products/"):
            return json.dumps(
                {
                    "object": "product",
                    "id": "prod_support",
                    "active": True,
                    "attributes": [],
                    "created": 0,
                    "default_price": None,
                    "description": None,
                    "images": [],
                    "livemode": False,
                    "marketing_features": [],
                    "metadata": {"product_type": product_type},
                    "name": "Support Plan",
                    "statement_descriptor": None,
                    "tax_code": None,
                    "type": "service",
                    "unit_label": None,
                    "updated": 0,
                    "url": None,
                }
            )
        raise AssertionError(f"unexpected stripe_get path: {path}")

    return _side_effect


@override_settings(STRIPE_REGION="")
class SupportWelcomeEmailTestCase(TestCase):
    def setUp(self):
        self.request = RequestFactory().post("/")

    async def test_sends_welcome_and_marks_stripe(self):
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            await update_subscription(build_subscription(), self.request)

        # Idempotency is marked on the Stripe subscription, not our DB.
        mark.assert_awaited_once_with(SUPPORT_SUB_ID)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(SUPPORT_SUB_ID, body)
        # The deep link carries only the key; the billing email is never
        # embedded in the link or sent in the body (in either alternative).
        for content in (body, html):
            self.assertIn(f"#sub={SUPPORT_SUB_ID}", content)
            self.assertNotIn("buyer@example.com", content)
            self.assertNotIn("&email=", content)
            self.assertNotIn("#email=", content)

    async def test_already_marked_skips_send(self):
        # welcome_sent rides in on the webhook payload, so the check is free and
        # short-circuits before the product fetch.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ) as get,
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            await update_subscription(
                build_subscription(metadata={"welcome_sent": "true"}), self.request
            )

        self.assertEqual(len(mail.outbox), 0)
        mark.assert_not_awaited()
        fetched = [call.args[0] for call in get.await_args_list]
        self.assertFalse(any(path.startswith("products/") for path in fetched))

    async def test_non_support_product_sends_nothing(self):
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(product_type="hosted"),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            await update_subscription(build_subscription(), self.request)

        self.assertEqual(len(mail.outbox), 0)
        mark.assert_not_awaited()

    async def test_inactive_status_defers_welcome(self):
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            await update_subscription(
                build_subscription(status=SubscriptionStatus.INCOMPLETE),
                self.request,
            )
        self.assertEqual(len(mail.outbox), 0)
        mark.assert_not_awaited()

        # Becomes active later -> welcome fires once.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch("apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)

    async def test_missing_customer_email_is_terminal_and_alerts(self):
        # Active support sub with no customer email: retrying the same payload
        # can't help, so we don't raise (the webhook 200s and Stripe stops
        # retrying) and alert with the sub id only — never the email.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(email=None),
            ),
            patch("apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock),
            self.assertLogs("apps.stripe.views", level="ERROR") as logs,
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 0)
        output = "\n".join(logs.output)
        self.assertIn(SUPPORT_SUB_ID, output)
        self.assertNotIn("buyer@example.com", output)

        # A later event that carries an email is a fresh delivery and sends once.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch("apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock),
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)

    async def test_send_failure_reraises_and_does_not_mark(self):
        # A transient send failure must re-raise (so the webhook 500s and Stripe
        # retries) and must not mark welcome_sent — the next attempt re-sends.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.email.SupportLicenseWelcomeEmail.send_email",
                side_effect=RuntimeError("smtp down"),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            with self.assertRaises(RuntimeError):
                await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 0)
        mark.assert_not_awaited()

        # A later working delivery sends and marks exactly once.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent", new_callable=AsyncMock
            ) as mark,
        ):
            await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)
        mark.assert_awaited_once_with(SUPPORT_SUB_ID)

    async def test_mark_failure_after_send_reraises(self):
        # send-then-mark: the email already went out, then the mark fails. We
        # re-raise so Stripe retries; the retry re-sends (a benign duplicate).
        # This is the at-least-once tradeoff — better a dupe than a lost key.
        with (
            patch(
                "apps.stripe.views.stripe_get",
                new_callable=AsyncMock,
                side_effect=stripe_get_side_effect(),
            ),
            patch(
                "apps.stripe.views.mark_welcome_sent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("stripe down"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                await update_subscription(build_subscription(), self.request)
        self.assertEqual(len(mail.outbox), 1)

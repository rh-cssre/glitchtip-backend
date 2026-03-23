import hmac
import json
import time
from unittest.mock import AsyncMock, patch

from dateutil.relativedelta import relativedelta
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.organizations_ext.models import Organization
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.views import stripe_webhook_view


class TestStripeWebhookCycle(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.url = reverse("stripe_webhook")
        self.webhook_secret = "test_webhook_secret"

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

    @override_settings(STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_REGION="")
    async def test_annual_plan_cycle(self):
        organization = await Organization.objects.acreate(name="Test Org", id=1)
        product = await StripeProduct.objects.acreate(
            stripe_id="prod_annual", name="Annual Product", events=1000, is_public=True
        )
        price = await StripePrice.objects.acreate(
            stripe_id="price_annual",
            product=product,
            price=120.00,
            nickname="Annual",
            interval="year",
        )

        now = timezone.now()
        start_ts = int(now.timestamp())
        # One year later
        end_ts = int((now + relativedelta(years=1)).timestamp())

        payload = {
            "type": "customer.subscription.updated",
            "id": "evt_test",
            "data": {
                "object": {
                    "object": "subscription",
                    "id": "sub_test_annual",
                    "customer": "cus_test",
                    "items": {
                        "object": "list",
                        "data": [
                            {
                                "id": "si_test",
                                "object": "subscription_item",
                                "price": {
                                    "id": price.stripe_id,
                                    "product": product.stripe_id,
                                    "recurring": {"interval": "year"},
                                    "unit_amount": 12000,
                                    "object": "price",
                                    "active": True,
                                    "created": start_ts,
                                    "currency": "usd",
                                    "livemode": False,
                                    "type": "recurring",
                                    "billing_scheme": "per_unit",
                                    "lookup_key": None,
                                    "nickname": "Annual",
                                    "tax_behavior": "unspecified",
                                    "tiers_mode": None,
                                    "unit_amount_decimal": "12000",
                                    "metadata": {},
                                },
                                "quantity": 1,
                                "subscription": "sub_test_annual",
                                "tax_rates": [],
                                "metadata": {},
                                "created": start_ts,
                                "current_period_start": start_ts,
                                "current_period_end": end_ts,
                            }
                        ],
                    },
                    "status": "active",
                    "created": start_ts,
                    "start_date": start_ts,
                    "collection_method": "charge_automatically",
                    "current_period_start": start_ts,
                    "current_period_end": end_ts,
                    "metadata": {},
                    "livemode": False,
                    "cancel_at_period_end": False,
                }
            },
            "api_version": "",
            "created": start_ts,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idempotency_key": "key"},
        }

        mock_customer_data = {
            "object": "customer",
            "id": "cus_test",
            "metadata": {"organization_id": str(organization.id)},
            "email": "test@example.com",
            "name": "Test User",
        }

        request = self.generate_stripe_request(payload)

        with patch("apps.stripe.views.stripe_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = json.dumps(mock_customer_data)
            await stripe_webhook_view(request)

        sub = await StripeSubscription.objects.aget(stripe_id="sub_test_annual")

        self.assertIsNotNone(
            sub.subscription_cycle_start, "Cycle start should not be None"
        )
        self.assertIsNotNone(sub.subscription_cycle_end, "Cycle end should not be None")

        # Verify cycle end is roughly one month later
        expected_cycle_end = sub.subscription_cycle_start + relativedelta(months=1)
        self.assertEqual(sub.subscription_cycle_end, expected_cycle_end)

    @override_settings(STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_REGION="")
    async def test_monthly_plan_cycle(self):
        organization = await Organization.objects.acreate(name="Test Org 2", id=2)
        product = await StripeProduct.objects.acreate(
            stripe_id="prod_monthly",
            name="Monthly Product",
            events=1000,
            is_public=True,
        )
        price = await StripePrice.objects.acreate(
            stripe_id="price_monthly",
            product=product,
            price=10.00,
            nickname="Monthly",
            interval="month",
        )

        now = timezone.now()
        start_ts = int(now.timestamp())
        # One month later
        end_ts = int((now + relativedelta(months=1)).timestamp())

        payload = {
            "type": "customer.subscription.updated",
            "id": "evt_test_monthly",
            "data": {
                "object": {
                    "object": "subscription",
                    "id": "sub_test_monthly",
                    "customer": "cus_test_monthly",
                    "items": {
                        "object": "list",
                        "data": [
                            {
                                "id": "si_test_monthly",
                                "object": "subscription_item",
                                "price": {
                                    "id": price.stripe_id,
                                    "product": product.stripe_id,
                                    "recurring": {"interval": "month"},
                                    "unit_amount": 1000,
                                    "object": "price",
                                    "active": True,
                                    "created": start_ts,
                                    "currency": "usd",
                                    "livemode": False,
                                    "type": "recurring",
                                    "billing_scheme": "per_unit",
                                    "lookup_key": None,
                                    "nickname": "Monthly",
                                    "tax_behavior": "unspecified",
                                    "tiers_mode": None,
                                    "unit_amount_decimal": "1000",
                                    "metadata": {},
                                },
                                "quantity": 1,
                                "subscription": "sub_test_monthly",
                                "tax_rates": [],
                                "metadata": {},
                                "created": start_ts,
                                "current_period_start": start_ts,
                                "current_period_end": end_ts,
                            }
                        ],
                    },
                    "status": "active",
                    "created": start_ts,
                    "start_date": start_ts,
                    "collection_method": "charge_automatically",
                    "current_period_start": start_ts,
                    "current_period_end": end_ts,
                    "metadata": {},
                    "livemode": False,
                    "cancel_at_period_end": False,
                }
            },
            "api_version": "",
            "created": start_ts,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idempotency_key": "key_monthly"},
        }

        mock_customer_data = {
            "object": "customer",
            "id": "cus_test_monthly",
            "metadata": {"organization_id": str(organization.id)},
            "email": "test@example.com",
            "name": "Test User",
        }

        request = self.generate_stripe_request(payload)

        with patch("apps.stripe.views.stripe_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = json.dumps(mock_customer_data)
            await stripe_webhook_view(request)

        sub = await StripeSubscription.objects.aget(stripe_id="sub_test_monthly")

        self.assertIsNotNone(
            sub.subscription_cycle_start, "Cycle start should not be None"
        )
        self.assertIsNotNone(sub.subscription_cycle_end, "Cycle end should not be None")

        # Verify cycle end matches period end for monthly plan
        self.assertEqual(sub.subscription_cycle_end, sub.current_period_end)

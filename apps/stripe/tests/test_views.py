import hmac
import json
import time
from unittest.mock import AsyncMock, patch

from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.views import stripe_webhook_view


class TestStripeWebhookView(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse("stripe_webhook")
        self.webhook_secret = "test_webhook_secret"  # Use a test secret

    @override_settings(STRIPE_WEBHOOK_SECRET=None)
    async def test_webhook_no_secret(self):
        """Test with a missing Stripe secret."""
        payload = b"{}"  # Empty payload
        headers = {"HTTP_STRIPE_SIGNATURE": "t=123,v1=some_signature"}
        request = self.factory.post(
            self.url, data=payload, content_type="application/json", **headers
        )
        response = await stripe_webhook_view(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"Invalid signature")

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_WEBHOOK_TOLERANCE=300
    )
    async def test_webhook_incorrect_secret(self):
        """Test with an incorrect Stripe secret."""
        payload = b'{"type": "test_event"}'
        # Create a signature with an INCORRECT secret
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        incorrect_signature = hmac.new(
            b"wrong_secret", signed_payload.encode("utf-8"), digestmod="sha256"
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={incorrect_signature}"}
        request = self.factory.post(
            self.url, data=payload, content_type="application/json", **headers
        )

        response = await stripe_webhook_view(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"Invalid signature")

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_WEBHOOK_TOLERANCE=300
    )
    async def test_webhook_unsupported_event_type(self):
        """Test a valid signature, but with an unsupported event type."""

        # Create a valid payload with an unsupported event type
        payload = json.dumps(
            {"type": "some.unsupported.event", "data": {"object": {}}}
        ).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}
        request = self.factory.post(
            self.url, data=payload, content_type="application/json", **headers
        )
        with patch("apps.stripe.views.logger") as mock_logger:
            response = await stripe_webhook_view(request)
            self.assertEqual(response.status_code, 200)
            mock_logger.warning.assert_called_once()

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_WEBHOOK_TOLERANCE=300
    )
    async def test_webhook_product_created(self):
        """Test the 'product.created' webhook."""

        payload = {
            "type": "product.created",
            "id": "evt_test",
            "data": {
                "object": {
                    "object": "product",
                    "id": "prod_test",
                    "active": True,
                    "attributes": [],
                    "created": 1678886400,
                    "default_price": None,
                    "description": "Test Description",
                    "images": [],
                    "livemode": False,
                    "marketing_features": [],
                    "metadata": {"events": "1", "is_public": "true"},
                    "name": "Test Product",
                    "statement_descriptor": None,
                    "tax_code": None,
                    "type": "service",
                    "unit_label": None,
                    "updated": 1678886400,
                    "url": None,
                }
            },
            "api_version": "2022-08-01",
            "created": 1678886401,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idemoptency_key": "test_key"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}
        request = self.factory.post(
            self.url, data=payload_bytes, content_type="application/json", **headers
        )

        response = await stripe_webhook_view(request)
        self.assertEqual(response.status_code, 200)

        product = await StripeProduct.objects.aget(stripe_id="prod_test")
        self.assertEqual(product.name, "Test Product")
        self.assertEqual(product.description, "Test Description")
        self.assertEqual(product.events, 1)

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_WEBHOOK_TOLERANCE=300
    )
    async def test_webhook_price_created(self):
        """Test the 'price.created' webhook."""

        # Create a product first.  This is REQUIRED for the price webhook to work.
        product = await StripeProduct.objects.acreate(
            stripe_id="prod_test_price",
            name="Test Product for Price",
            description="Test Description",
            events=1,
            is_public=True,
        )

        payload = {
            "type": "price.created",
            "id": "evt_test_price",
            "data": {
                "object": {
                    "object": "price",
                    "id": "price_test",
                    "active": True,
                    "billing_scheme": "per_unit",
                    "created": 1678886400,
                    "currency": "usd",
                    "livemode": False,
                    "lookup_key": None,
                    "nickname": "Test Price",
                    "product": product.stripe_id,
                    "recurring": None,
                    "tax_behavior": "unspecified",
                    "tiers_mode": None,
                    "type": "one_time",
                    "unit_amount": 2000,  # $20.00
                    "unit_amount_decimal": "2000",
                    "metadata": {},
                }
            },
            "api_version": "2022-08-01",
            "created": 1678886401,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idemoptency_key": "test_key"},
        }

        payload_bytes = json.dumps(payload).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}
        request = self.factory.post(
            self.url, data=payload_bytes, content_type="application/json", **headers
        )

        response = await stripe_webhook_view(request)
        self.assertEqual(response.status_code, 200)

        # Check that the Price object was created and associated with the Product
        price = await StripePrice.objects.aget(stripe_id="price_test")
        self.assertEqual(price.price, 20.00)  # Check decimal conversion
        self.assertEqual(price.nickname, "Test Price")

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret", STRIPE_WEBHOOK_TOLERANCE=300
    )
    async def test_webhook_subscription_created(self):
        """Test the 'customer.subscription.created' webhook."""

        # 1. Create a related Organization.
        organization = await Organization.objects.acreate(
            name="Test Org",
            id=12345,  # Use a known ID for the test
        )

        # 2. Create a related Product
        product = await StripeProduct.objects.acreate(
            stripe_id="prod_test_sub",
            name="Test Product for Subscription",
            description="Test Description",
            events=1,
            is_public=True,
        )
        # 3. Create a related price
        price = await StripePrice.objects.acreate(
            stripe_id="price_test", product=product, price=10.00, nickname="Test Price"
        )

        # 4. Construct the payload.
        payload = {
            "type": "customer.subscription.created",
            "id": "evt_test_subscription",
            "data": {
                "object": {
                    "object": "subscription",
                    "id": "sub_test",
                    "customer": "cus_test",  # Mocked later
                    "items": {
                        "object": "list",
                        "data": [
                            {
                                "id": "si_test",
                                "price": {
                                    "id": price.stripe_id,
                                    "product": price.product_id,
                                },
                                "plan": {"product": "prod_test_sub"},
                            }
                        ],
                    },
                    "created": 1678886400,
                    "current_period_start": 1678886400,
                    "current_period_end": 1681564800,
                    "status": "active",
                    "livemode": False,
                    "metadata": {},
                    "cancel_at_period_end": False,
                    "start_date": 1678886400,
                    "collection_method": "charge_automatically"
                }
            },
            "api_version": "2022-08-01",
            "created": 1678886401,
            "livemode": False,
            "pending_webhooks": 1,
            "request": {"id": "req_test", "idemoptency_key": "test_key"},
        }
        # Mock Customer data for stripe_get.
        mock_customer_data = {
            "object": "customer",
            "id": "cus_test",
            "email": "test@example.com",
            "metadata": {"organization_id": str(organization.id)},
            "name": None,
        }

        payload_bytes = json.dumps(payload).encode("utf-8")
        timestamp = int(time.time())
        signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()

        headers = {"HTTP_STRIPE_SIGNATURE": f"t={timestamp},v1={signature}"}
        request = self.factory.post(
            self.url, data=payload_bytes, content_type="application/json", **headers
        )

        # Use AsyncMock for the asynchronous stripe_get function.
        with patch(
            "apps.stripe.views.stripe_get", new_callable=AsyncMock
        ) as mock_stripe_get:
            mock_stripe_get.return_value = json.dumps(
                mock_customer_data
            )  # Return JSON string
            response = await stripe_webhook_view(request)
            self.assertEqual(response.status_code, 200)

            # Assert stripe_get was called correctly.
            mock_stripe_get.assert_awaited_once_with("customers/cus_test")

        # 5. Verify the StripeSubscription was created.
        subscription = await StripeSubscription.objects.aget(stripe_id="sub_test")
        self.assertEqual(subscription.status, SubscriptionStatus.ACTIVE)

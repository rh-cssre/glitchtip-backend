from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from ..constants import SubscriptionStatus
from ..models import StripeProduct, StripeSubscription
from ..schema import (
    Customer,
    Items,
    Price,
    ProductExpandedPrice,
    SubscriptionExpandCustomer,
)

test_price = Price(
    object="price",
    id="price_1",
    active=True,
    unit_amount=1000,
    currency="usd",
    type="one_time",
    metadata={},
    billing_scheme="per_unit",
    created=1678886400,
    livemode=False,
    lookup_key=None,
    nickname=None,
    product="prod_1",
    recurring=None,
    tax_behavior=None,
    tiers_mode=None,
    unit_amount_decimal="1000",
)


class StripeTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = baker.make("organizations_ext.Organization")

    @patch("apps.stripe.models.list_products")
    async def test_sync_product(self, mock_list_products):
        mock_products_page_1 = [
            ProductExpandedPrice(
                object="product",
                id="prod_1",
                active=True,
                attributes=[],
                created=1678886400,
                default_price=test_price,
                description="Description 1",
                images=[],
                livemode=False,
                marketing_features=[],
                metadata={"events": "123", "is_public": "true"},
                name="Product 1",
                package_dimensions=None,
                shippable=None,
                statement_descriptor=None,
                tax_code=None,
                type="service",
                unit_label=None,
                updated=1678886400,
                url=None,
            ),
        ]

        async def mock_products_generator():
            yield mock_products_page_1

        mock_list_products.return_value = mock_products_generator()
        await StripeProduct.sync_from_stripe()

        self.assertEqual(
            await StripeProduct.objects.acount(), len(mock_products_page_1)
        )

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret",
        STRIPE_WEBHOOK_TOLERANCE=300,
        STRIPE_REGION="",
    )
    @patch("apps.stripe.models.list_subscriptions")
    async def test_sync_subscription(self, mock_list_subscriptions):
        await sync_to_async(baker.make)("stripe.StripePrice", stripe_id=test_price.id)
        await sync_to_async(baker.make)(
            "stripe.StripeProduct", stripe_id=test_price.product
        )

        now = timezone.now()
        now_timestamp = int(now.timestamp())
        subscriptions_page_1 = [
            SubscriptionExpandCustomer(
                object="subscription",
                id="sub_1",
                customer=Customer(
                    object="customer",
                    id="cus_1",
                    email="foo@example.com",
                    metadata={},
                    name="",
                ),
                items=Items(
                    object="list",
                    data=[
                        {
                            "price": {
                                "id": test_price.id,
                                "product": test_price.product,
                                "unit_amount": test_price.unit_amount,
                            }
                        }
                    ],
                ),
                created=now_timestamp,
                current_period_end=now_timestamp + 2592000,  # +30 days
                current_period_start=now_timestamp,
                status=SubscriptionStatus.ACTIVE,
                livemode=False,
                metadata={},
                cancel_at_period_end=False,
                start_date=now_timestamp,
                collection_method="charge_automatically",
            )
        ]

        async def mock_subscriptions_generator():
            yield subscriptions_page_1

        mock_list_subscriptions.return_value = mock_subscriptions_generator()
        await StripeSubscription.sync_from_stripe()

        # Subscription without valid organization_id in customer metadata should be skipped
        self.assertEqual(await StripeSubscription.objects.acount(), 0)

        subscriptions_page_1[0].customer.metadata = {
            "organization_id": str(self.org.id)
        }
        mock_list_subscriptions.return_value = mock_subscriptions_generator()
        await StripeSubscription.sync_from_stripe()

        self.assertEqual(
            await StripeSubscription.objects.acount(), len(subscriptions_page_1)
        )

    @override_settings(
        STRIPE_WEBHOOK_SECRET="test_webhook_secret",
        STRIPE_WEBHOOK_TOLERANCE=300,
        STRIPE_REGION="",
    )
    @patch("apps.stripe.models.list_subscriptions")
    @patch("apps.stripe.models.fetch_subscription")
    async def test_sync_removes_canceled_primary_subscriptions(
        self, mock_fetch_subscription, mock_list_subscriptions
    ):
        await sync_to_async(baker.make)("stripe.StripePrice", stripe_id=test_price.id)
        await sync_to_async(baker.make)(
            "stripe.StripeProduct", stripe_id=test_price.product
        )

        subscription = await sync_to_async(baker.make)(
            "stripe.StripeSubscription",
            stripe_id=test_price.product,
            organization=self.org,
            current_period_end=timezone.now() - timedelta(days=3)
        )

        self.org.stripe_primary_subscription = subscription
        await self.org.asave()

        created_timestamp = int(subscription.created.timestamp())

        subscription_data = SubscriptionExpandCustomer(
            object="subscription",
            id="sub_1",
            customer=Customer(
                object="customer",
                id="cus_1",
                email="foo@example.com",
                metadata={},
                name="",
            ),
            items=Items(
                object="list",
                data=[
                    {
                        "price": {
                            "id": test_price.id,
                            "product": test_price.product,
                            "unit_amount": test_price.unit_amount,
                        }
                    }
                ],
            ),
            created=created_timestamp,
            current_period_end=created_timestamp - 259200,  # -3 days
            current_period_start=created_timestamp,
            status=SubscriptionStatus.CANCELED,
            livemode=False,
            metadata={},
            cancel_at_period_end=False,
            start_date=created_timestamp,
            collection_method="charge_automatically",
        )

        async def mock_subscriptions_generator():
            yield []

        mock_list_subscriptions.return_value = mock_subscriptions_generator()
        mock_fetch_subscription.return_value = subscription_data
        await StripeSubscription.sync_from_stripe()


        await self.org.arefresh_from_db()
        await subscription.arefresh_from_db()
        mock_fetch_subscription.assert_called_once()
        self.assertFalse(self.org.stripe_primary_subscription)
        self.assertEqual(subscription.status, SubscriptionStatus.CANCELED)

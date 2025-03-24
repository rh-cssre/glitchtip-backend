from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.test import TestCase
from model_bakery import baker

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

    @patch("apps.stripe.models.list_subscriptions")
    async def test_sync_subscription(self, mock_list_subscriptions):
        await sync_to_async(baker.make)("stripe.StripePrice", stripe_id=test_price.id)
        await sync_to_async(baker.make)(
            "stripe.StripeProduct", stripe_id=test_price.product
        )
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
                created=1678886400,
                current_period_end=1678886400 + 2592000,  # +30 days
                current_period_start=1678886400,
                status="active",
                livemode=False,
                metadata={},
                cancel_at_period_end=False,
                start_date=1678886400,
                collection_method="charge_automatically"
            )
        ]

        async def mock_subscriptions_generator():
            yield subscriptions_page_1

        mock_list_subscriptions.return_value = mock_subscriptions_generator()
        await StripeSubscription.sync_from_stripe()

        # Subscription without valid organization_id in customer metadata should be skipped
        self.assertEqual(
            await StripeSubscription.objects.acount(), 0
        )

        subscriptions_page_1[0].customer.metadata = {"organization_id": str(self.org.id)}
        mock_list_subscriptions.return_value = mock_subscriptions_generator()
        await StripeSubscription.sync_from_stripe()

        self.assertEqual(
            await StripeSubscription.objects.acount(), len(subscriptions_page_1)
        )

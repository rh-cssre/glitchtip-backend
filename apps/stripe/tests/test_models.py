from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from ..constants import SubscriptionStatus
from ..models import StripePrice, StripeProduct, StripeSubscription
from ..schema import (
    Customer,
    Price,
    ProductExpandedPrice,
    SubscriptionExpandCustomer,
    SubscriptionItem,
    SubscriptionItems,
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
                metadata={
                    "events": "123",
                    "is_public": "true",
                    "product_type": "hosted",
                },
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

    @patch("apps.stripe.models.list_products")
    async def test_sync_keeps_products_referenced_by_subscription(
        self, mock_list_products
    ):
        """A product Stripe no longer lists must not be deleted while a
        subscription still references one of its prices. StripeSubscription.price
        is RESTRICT, so deleting it would raise RestrictedError and abort sync."""
        # An archived product (absent from Stripe's response) with a price that a
        # live subscription still points at.
        old_product = await StripeProduct.objects.acreate(
            stripe_id="prod_old", name="Old", events=1000, is_public=False
        )
        old_price = await StripePrice.objects.acreate(
            stripe_id="price_old", product=old_product, price=10.00, nickname="Old"
        )
        now = timezone.now()
        await StripeSubscription.objects.acreate(
            stripe_id="sub_grandfathered",
            created=now,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
            price=old_price,
            organization=self.org,
            status=SubscriptionStatus.ACTIVE,
            start_date=now,
            collection_method="charge_automatically",
        )

        async def mock_products_generator():
            yield [
                ProductExpandedPrice(
                    object="product",
                    id="prod_current",
                    active=True,
                    attributes=[],
                    created=1678886400,
                    default_price=test_price,
                    description="Current",
                    images=[],
                    livemode=False,
                    marketing_features=[],
                    metadata={
                        "events": "123",
                        "is_public": "true",
                        "product_type": "hosted",
                    },
                    name="Current",
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

        mock_list_products.return_value = mock_products_generator()
        # Must not raise RestrictedError.
        await StripeProduct.sync_from_stripe()

        # The referenced-but-archived product is retained.
        self.assertTrue(
            await StripeProduct.objects.filter(stripe_id="prod_old").aexists()
        )

    @patch("apps.stripe.models.list_products")
    async def test_sync_deletes_unreferenced_archived_products(
        self, mock_list_products
    ):
        """A product absent from Stripe with no subscription referencing it is
        still pruned — the retention guard must not block ordinary cleanup."""
        await StripeProduct.objects.acreate(
            stripe_id="prod_stale", name="Stale", events=1000, is_public=False
        )

        async def mock_products_generator():
            yield [
                ProductExpandedPrice(
                    object="product",
                    id="prod_current",
                    active=True,
                    attributes=[],
                    created=1678886400,
                    default_price=test_price,
                    description="Current",
                    images=[],
                    livemode=False,
                    marketing_features=[],
                    metadata={
                        "events": "123",
                        "is_public": "true",
                        "product_type": "hosted",
                    },
                    name="Current",
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

        mock_list_products.return_value = mock_products_generator()
        await StripeProduct.sync_from_stripe()

        self.assertFalse(
            await StripeProduct.objects.filter(stripe_id="prod_stale").aexists()
        )

    @patch("apps.stripe.models.list_products")
    async def test_sync_product_round_trips_price_is_public(self, mock_list_products):
        public_price = test_price.model_copy(
            update={"id": "price_pub", "metadata": {"is_public": "true"}}
        )
        private_price = test_price.model_copy(
            update={"id": "price_priv", "metadata": {}}
        )

        async def mock_products_generator():
            yield [
                ProductExpandedPrice(
                    object="product",
                    id="prod_pub",
                    active=True,
                    attributes=[],
                    created=1678886400,
                    default_price=public_price,
                    description="",
                    images=[],
                    livemode=False,
                    marketing_features=[],
                    metadata={
                        "events": "123",
                        "is_public": "true",
                        "product_type": "hosted",
                    },
                    name="Public",
                    package_dimensions=None,
                    shippable=None,
                    statement_descriptor=None,
                    tax_code=None,
                    type="service",
                    unit_label=None,
                    updated=1678886400,
                    url=None,
                ),
                ProductExpandedPrice(
                    object="product",
                    id="prod_priv",
                    active=True,
                    attributes=[],
                    created=1678886400,
                    default_price=private_price,
                    description="",
                    images=[],
                    livemode=False,
                    marketing_features=[],
                    metadata={
                        "events": "123",
                        "is_public": "true",
                        "product_type": "hosted",
                    },
                    name="Has Private Default",
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

        mock_list_products.return_value = mock_products_generator()
        await StripeProduct.sync_from_stripe()

        self.assertTrue(
            (await StripePrice.objects.aget(stripe_id="price_pub")).is_public
        )
        self.assertFalse(
            (await StripePrice.objects.aget(stripe_id="price_priv")).is_public
        )

    @patch("apps.stripe.models.list_prices")
    async def test_sync_price_round_trips_is_public(self, mock_list_prices):
        await sync_to_async(baker.make)("stripe.StripeProduct", stripe_id="prod_1")

        async def mock_prices_generator():
            yield [
                test_price.model_copy(
                    update={"id": "price_pub", "metadata": {"is_public": "true"}}
                ),
                test_price.model_copy(
                    update={"id": "price_mixed_case", "metadata": {"is_public": "True"}}
                ),
                test_price.model_copy(update={"id": "price_priv", "metadata": {}}),
                test_price.model_copy(
                    update={"id": "price_other", "metadata": {"no_throttle": "true"}}
                ),
                test_price.model_copy(
                    update={"id": "price_one", "metadata": {"is_public": "1"}}
                ),
            ]

        mock_list_prices.return_value = mock_prices_generator()
        await StripePrice.sync_from_stripe()

        self.assertTrue(
            (await StripePrice.objects.aget(stripe_id="price_pub")).is_public
        )
        # Case-insensitive predicate, mirrors no_throttle behavior
        self.assertTrue(
            (await StripePrice.objects.aget(stripe_id="price_mixed_case")).is_public
        )
        self.assertFalse(
            (await StripePrice.objects.aget(stripe_id="price_priv")).is_public
        )
        self.assertFalse(
            (await StripePrice.objects.aget(stripe_id="price_other")).is_public
        )
        # Only literal "true" counts — "1" must not be treated as truthy
        self.assertFalse(
            (await StripePrice.objects.aget(stripe_id="price_one")).is_public
        )

    @patch("apps.stripe.models.logger")
    @patch("apps.stripe.models.list_prices")
    async def test_sync_price_warns_on_duplicate_public_prices(
        self, mock_list_prices, mock_logger
    ):
        await sync_to_async(baker.make)("stripe.StripeProduct", stripe_id="prod_1")

        async def mock_prices_generator():
            yield [
                test_price.model_copy(
                    update={
                        "id": "price_a",
                        "metadata": {"is_public": "true"},
                        "recurring": {"interval": "month"},
                    }
                ),
                test_price.model_copy(
                    update={
                        "id": "price_b",
                        "metadata": {"is_public": "true"},
                        "recurring": {"interval": "month"},
                    }
                ),
            ]

        mock_list_prices.return_value = mock_prices_generator()
        await StripePrice.sync_from_stripe()

        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args.args
        self.assertIn("prod_1", warning_args)
        self.assertIn("month", warning_args)

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
                items=SubscriptionItems(
                    object="list",
                    data=[
                        SubscriptionItem(
                            id="test_subscription_item",
                            object="subscription_item",
                            created=now_timestamp,
                            current_period_end=now_timestamp + 2592000,  # +30 days
                            current_period_start=now_timestamp,
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
                                unit_amount_decimal=str(test_price.unit_amount),
                                metadata={},
                                id=test_price.id,
                                product=test_price.product,
                                unit_amount=test_price.unit_amount,
                            ),
                            quantity=1,
                            subscription="subscription_id",
                            tax_rates=[],
                        )
                    ],
                ),
                created=now_timestamp,
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
            current_period_end=timezone.now() - timedelta(days=3),
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
            items=SubscriptionItems(
                object="list",
                data=[
                    SubscriptionItem(
                        id="test_subscription_item",
                        object="subscription_item",
                        created=created_timestamp,
                        current_period_end=created_timestamp - 259200,  # -3 days
                        current_period_start=created_timestamp,
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
                            unit_amount_decimal=str(test_price.unit_amount),
                            metadata={},
                            id=test_price.id,
                            product=test_price.product,
                            unit_amount=test_price.unit_amount,
                        ),
                        quantity=1,
                        subscription="subscription_id",
                        tax_rates=[],
                    )
                ],
            ),
            created=created_timestamp,
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

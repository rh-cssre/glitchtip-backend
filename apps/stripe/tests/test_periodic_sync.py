import datetime
from unittest.mock import AsyncMock, patch

from dateutil.relativedelta import relativedelta
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.schema import Price, Subscription, SubscriptionItem, SubscriptionItems


class TestPeriodicSync(TestCase):
    @override_settings(STRIPE_WEBHOOK_SECRET="test")
    async def test_update_outdated_subscriptions_cycles(self):
        # Setup initial state with an outdated subscription
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
        # Initial period was 1 year ago, ending 3 days ago (so it's outdated)
        start_ts = int((now - relativedelta(years=1, days=3)).timestamp())
        end_ts = int((now - relativedelta(days=3)).timestamp())

        subscription = await StripeSubscription.objects.acreate(
            stripe_id="sub_outdated",
            created=datetime.datetime.fromtimestamp(start_ts, tz=datetime.timezone.utc),
            current_period_start=datetime.datetime.fromtimestamp(
                start_ts, tz=datetime.timezone.utc
            ),
            current_period_end=datetime.datetime.fromtimestamp(
                end_ts, tz=datetime.timezone.utc
            ),
            price=price,
            organization=organization,
            status=SubscriptionStatus.ACTIVE,
            start_date=datetime.datetime.fromtimestamp(
                start_ts, tz=datetime.timezone.utc
            ),
            collection_method="charge_automatically",
            subscription_cycle_start=datetime.datetime.fromtimestamp(
                start_ts, tz=datetime.timezone.utc
            ),
            # Cycle end was 1 month after start
            subscription_cycle_end=datetime.datetime.fromtimestamp(
                start_ts, tz=datetime.timezone.utc
            )
            + relativedelta(months=1),
        )

        # Mock the response from Stripe for the renewal
        new_start_ts = int((now - relativedelta(days=3)).timestamp())
        new_end_ts = int((now + relativedelta(years=1, days=-3)).timestamp())

        mock_sub_data = Subscription(
            object="subscription",
            id="sub_outdated",
            customer="cus_test",
            status="active",
            created=new_start_ts,
            start_date=new_start_ts,
            collection_method="charge_automatically",
            items=SubscriptionItems(
                object="list",
                data=[
                    SubscriptionItem(
                        id="si_test",
                        object="subscription_item",
                        created=new_start_ts,
                        current_period_start=new_start_ts,
                        current_period_end=new_end_ts,
                        metadata={},
                        quantity=1,
                        subscription="sub_outdated",
                        tax_rates=[],
                        price=Price(
                            id=price.stripe_id,
                            object="price",
                            active=True,
                            billing_scheme="per_unit",
                            created=start_ts,
                            currency="usd",
                            livemode=False,
                            lookup_key=None,
                            nickname="Annual",
                            product=product.stripe_id,
                            recurring={"interval": "year"},
                            tax_behavior="unspecified",
                            tiers_mode=None,
                            type="recurring",
                            unit_amount=12000,
                            unit_amount_decimal="12000",
                            metadata={},
                        ),
                    )
                ],
            ),
            metadata={},
            livemode=False,
            cancel_at_period_end=False,
        )

        with patch(
            "apps.stripe.models.fetch_subscription", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = mock_sub_data

            await StripeSubscription.update_outdated_subscriptions()

        # Refresh from DB
        subscription = await StripeSubscription.objects.aget(stripe_id="sub_outdated")

        # Check if period was updated
        self.assertEqual(subscription.current_period_end.timestamp(), new_end_ts)

        # Check if cycles were updated (THIS IS WHAT WE EXPECT TO FAIL)
        self.assertEqual(
            subscription.subscription_cycle_start.timestamp(), new_start_ts
        )
        expected_cycle_end = subscription.subscription_cycle_start + relativedelta(
            months=1
        )
        self.assertEqual(subscription.subscription_cycle_end, expected_cycle_end)

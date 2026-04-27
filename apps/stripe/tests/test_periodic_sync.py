import datetime
from unittest.mock import AsyncMock, patch

from dateutil.relativedelta import relativedelta
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripePrice, StripeProduct, StripeSubscription
from apps.stripe.schema import Price, Subscription, SubscriptionItem, SubscriptionItems
from apps.stripe.utils import unix_to_datetime


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
        self.assertEqual(subscription.current_period_end, unix_to_datetime(new_end_ts))

        # Check if cycles were updated — period started 3 days ago so we're
        # in month 1 and cycle_start should equal period_start.
        expected_cycle_start = unix_to_datetime(new_start_ts)
        self.assertEqual(subscription.subscription_cycle_start, expected_cycle_start)
        self.assertEqual(
            subscription.subscription_cycle_end,
            expected_cycle_start + relativedelta(months=1),
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="test")
    async def test_update_outdated_subscriptions_mid_year_cycle(self):
        """Annual sub 5 months into its period should get a cycle covering now,
        not month 1.  Reproduces the bug where cycle_end was always set to
        period_start + 1 month regardless of how far into the year we are."""
        organization = await Organization.objects.acreate(name="Test Org Mid", id=2)
        product = await StripeProduct.objects.acreate(
            stripe_id="prod_annual_mid",
            name="Annual Product",
            events=1000,
            is_public=True,
        )
        price = await StripePrice.objects.acreate(
            stripe_id="price_annual_mid",
            product=product,
            price=120.00,
            nickname="Annual",
            interval="year",
        )

        now = timezone.now()
        # Annual billing period started 5 months ago
        period_start = now - relativedelta(months=5)
        period_end = period_start + relativedelta(years=1)
        # DB has a stale current_period_end (> 2 days ago triggers the filter)
        stale_period_end = now - relativedelta(months=6)

        period_start_ts = int(period_start.timestamp())
        period_end_ts = int(period_end.timestamp())

        subscription = await StripeSubscription.objects.acreate(
            stripe_id="sub_mid_year",
            created=period_start,
            current_period_start=period_start,
            current_period_end=stale_period_end,
            price=price,
            organization=organization,
            status=SubscriptionStatus.ACTIVE,
            start_date=period_start,
            collection_method="charge_automatically",
            subscription_cycle_start=period_start,
            subscription_cycle_end=period_start + relativedelta(months=1),
        )

        mock_sub_data = Subscription(
            object="subscription",
            id="sub_mid_year",
            customer="cus_test2",
            status="active",
            created=period_start_ts,
            start_date=period_start_ts,
            collection_method="charge_automatically",
            items=SubscriptionItems(
                object="list",
                data=[
                    SubscriptionItem(
                        id="si_test2",
                        object="subscription_item",
                        created=period_start_ts,
                        current_period_start=period_start_ts,
                        current_period_end=period_end_ts,
                        metadata={},
                        quantity=1,
                        subscription="sub_mid_year",
                        tax_rates=[],
                        price=Price(
                            id=price.stripe_id,
                            object="price",
                            active=True,
                            billing_scheme="per_unit",
                            created=period_start_ts,
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

        subscription = await StripeSubscription.objects.aget(stripe_id="sub_mid_year")

        # The cycle must cover the current moment — not be stuck at month 1.
        # Old buggy code set cycle_end = period_start + 1 month (4 months ago).
        self.assertGreaterEqual(subscription.subscription_cycle_end, now)
        self.assertLessEqual(subscription.subscription_cycle_start, now)
        # Boundaries must be anchored to current_period_start + N months (not
        # chained from cycle_start, which drifts on month-end dates like Jan 31).
        # Use the DB value (integer-timestamp precision) not the local variable.
        anchor = subscription.current_period_start
        diff = relativedelta(subscription.subscription_cycle_end, anchor)
        self.assertEqual(diff.days, 0, "cycle_end should be period_start + N months")
        diff = relativedelta(subscription.subscription_cycle_start, anchor)
        self.assertEqual(diff.days, 0, "cycle_start should be period_start + N months")

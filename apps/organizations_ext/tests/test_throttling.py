from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from asgiref.sync import async_to_sync
from dateutil.relativedelta import relativedelta
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription

from ..models import Organization
from ..tasks import (
    check_all_organizations_throttle,
    check_organization_throttle,
    get_free_tier_cycle,
    update_subscription_cycles,
)


class OrganizationThrottleCheckTestCase(TestCase):
    def setUp(self):
        self.product = baker.make("stripe.StripeProduct", events=10)
        self.price = baker.make("stripe.StripePrice", price=0, product=self.product)
        self.organization = baker.make("organizations_ext.Organization")
        self.user = baker.make("users.user")
        self.organization.add_user(self.user)
        self.subscription = baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            price=self.price,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.now() - timedelta(hours=1),
            current_period_end=timezone.now() + timedelta(hours=1),
            subscription_cycle_start=timezone.now() - timedelta(hours=1),
            subscription_cycle_end=timezone.now() + timedelta(hours=1),
        )
        self.organization.stripe_primary_subscription = self.subscription
        self.organization.save()

    def _make_events(self, i: int, date=None):
        if date is None:
            date = timezone.now()
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            organization=self.organization,
            count=i,
            date=date,
        )

    def _make_transaction_events(self, i: int, date=None):
        if date is None:
            date = timezone.now()
        baker.make(
            "projects.TransactionEventProjectHourlyStatistic",
            project__organization=self.organization,
            organization=self.organization,
            count=i,
            date=date,
        )

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    )
    def test_check_organization_throttle(self):
        check_organization_throttle.call(self.organization.id)
        self.assertTrue(Organization.objects.filter(event_throttle_rate=0).exists())

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=11,
        )
        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 10)
        self.assertEqual(len(mail.outbox), 1)

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=100,
        )
        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 100)
        self.assertEqual(len(mail.outbox), 2)

    def test_bypass_organization_throttle(self):
        """Ensure bypassing the check org throttle cache works"""
        check_organization_throttle.call(self.organization.id)
        self.assertTrue(cache.get(f"org-throttle-{self.organization.id}"))
        cache.clear()
        check_organization_throttle.call(self.organization.id, True)
        self.assertFalse(cache.get(f"org-throttle-{self.organization.id}"))
        cache.clear()

    def test_check_all_organizations_throttle(self):
        """
        Test throttle calculation with weighted event counts.

        Event weights: issues=1.0, transactions=1.0, logs=0.1
        Plan limit: 10 events
        Throttle thresholds: >100%=10%, >150%=50%, >200%=100%
        """
        org = self.organization

        # No events, no throttle
        with self.assertNumQueries(1):
            check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)

        # 6 weighted events (of 10), no throttle
        # 3 issues (3.0) + 3 transactions (3.0) = 6.0 weighted events
        self._make_events(3, date=timezone.now() - timedelta(minutes=50))
        self._make_transaction_events(3, date=timezone.now() - timedelta(minutes=45))
        check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)
        self.assertEqual(len(mail.outbox), 0)

        # 11 weighted events (of 10), small throttle (>100%)
        # Previous 6.0 + 5 issues (5.0) = 11.0 weighted events
        self._make_events(5, date=timezone.now() - timedelta(minutes=40))
        check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 10)
        self.assertEqual(len(mail.outbox), 1)

        # New time period, should reset throttle
        now = timezone.now()
        self.subscription.current_period_start = now + timedelta(minutes=1)
        self.subscription.current_period_end = now + timedelta(hours=1)
        self.subscription.subscription_cycle_start = now + timedelta(minutes=1)
        self.subscription.subscription_cycle_end = now + timedelta(hours=1)
        self.subscription.save()
        check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)
        self.assertEqual(len(mail.outbox), 1)

        # Throttle again (>150%)
        # 16 issues = 16.0 weighted events > 15 (150% of 10)
        self._make_events(16, date=now + timedelta(minutes=5))
        check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 50)

        # Throttle 100% (>200%)
        # Previous 16 + 5 = 21 > 20 (200% of 10)
        self._make_events(5, date=now + timedelta(minutes=10))
        check_all_organizations_throttle.call()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 100)

    def test_free_tier_throttle(self):
        """
        Verify free tier logic (default 1000 events)
        """
        self.subscription.delete()
        check_all_organizations_throttle.call()
        self.organization.refresh_from_db()
        self.assertEqual(
            self.organization.event_throttle_rate, 0
        )  # Should be 0 now (free tier)

        # Exceed free tier (1000)
        self._make_events(1001)
        check_all_organizations_throttle.call()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 10)

    @override_settings(BILLING_ENABLED=False)
    def test_self_hosted_no_throttle(self):
        """
        Verify that self-hosted instances (BILLING_ENABLED=False) are not throttled
        by the automated logic, even if they exceed limits.
        """
        self.subscription.delete()
        # Create massive amount of events
        self._make_events(10000)

        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()

        # Should remain 0 (or whatever it was manually set to, here default 0)
        self.assertEqual(self.organization.event_throttle_rate, 0)

        # Verify manual throttle is preserved
        self.organization.event_throttle_rate = 50
        self.organization.save()

        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 50)

    def test_no_plan_throttle(self):
        """
        It's possible to not sign up for a free plan, they should be throttled
        """
        self.subscription.delete()
        check_all_organizations_throttle.call()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)

        # Make plan active
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            price=self.price,
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() + timedelta(hours=1),
            subscription_cycle_start=timezone.now() - timedelta(hours=1),
            subscription_cycle_end=timezone.now() + timedelta(hours=1),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        check_all_organizations_throttle.call()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)

    def test_no_throttle_override(self):
        """Verify that price.no_throttle bypasses all throttling"""
        self.price.no_throttle = True
        self.price.save()
        self.subscription.price = self.price
        self.subscription.save()
        self.organization.refresh_from_db()
        self._make_events(100)  # Well over 10 event limit
        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)

    def test_yearly_throttle(self):
        """Verify that yearly plans enforce monthly quotas using virtual cycles"""
        self.price.interval = "year"
        self.price.save()

        # Set cycle to current month
        start_cycle = timezone.now() - timedelta(days=5)
        end_cycle = timezone.now() + timedelta(days=25)

        self.subscription.price = self.price
        self.subscription.subscription_cycle_start = start_cycle
        self.subscription.subscription_cycle_end = end_cycle
        self.subscription.save()
        self.organization.refresh_from_db()

        # Create events in the PREVIOUS month (should be ignored)
        self._make_events(100, date=start_cycle - timedelta(days=1))

        # Create events in CURRENT month (should be counted)
        # Limit is 10. 5 events -> OK.
        self._make_events(5, date=timezone.now())

        check_organization_throttle.call(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)  # 5 < 10

        # Add more events to exceed monthly limit
        self._make_events(10, date=timezone.now() + timedelta(minutes=10))  # Total 15
        check_organization_throttle.call(self.organization.id, bypass_cache=True)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 10)  # 15 > 10

    def test_update_subscription_cycles(self):
        """Verify that annual plans roll over their virtual month cycle"""
        self.price.interval = "year"
        self.price.save()

        # Set cycle to LAST month
        start_cycle = timezone.now() - relativedelta(months=1, days=1)
        end_cycle = start_cycle + relativedelta(months=1)  # Ends 1 day ago

        self.subscription.price = self.price
        self.subscription.subscription_cycle_start = start_cycle
        self.subscription.subscription_cycle_end = end_cycle
        self.subscription.save()

        update_subscription_cycles.call()

        self.subscription.refresh_from_db()
        # Should have advanced by 1 month
        self.assertEqual(self.subscription.subscription_cycle_start, end_cycle)
        self.assertEqual(
            self.subscription.subscription_cycle_end,
            end_cycle + relativedelta(months=1),
        )


class FreeTierCycleTestCase(TestCase):
    def test_get_free_tier_cycle(self):
        # Test basic case
        created = datetime(2020, 1, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
        with freeze_time("2020-03-10"):
            start, end = get_free_tier_cycle(created)
            self.assertEqual(
                start, datetime(2020, 2, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
            )
            self.assertEqual(
                end, datetime(2020, 3, 15, 12, 0, 0, tzinfo=dt_timezone.utc)
            )

        # Test edge case: Created Jan 31, Now Feb 10 (Leap year 2020)
        created = datetime(2020, 1, 31, 12, 0, 0, tzinfo=dt_timezone.utc)
        with freeze_time("2020-02-10"):
            start, end = get_free_tier_cycle(created)
            self.assertEqual(
                start, datetime(2020, 1, 31, 12, 0, 0, tzinfo=dt_timezone.utc)
            )
            self.assertEqual(
                end, datetime(2020, 2, 29, 12, 0, 0, tzinfo=dt_timezone.utc)
            )

        # Test edge case: Created Jan 31, Now Feb 28 (Leap year 2020)
        # Cycle should be Jan 31 - Feb 29
        with freeze_time("2020-02-28"):
            start, end = get_free_tier_cycle(created)
            self.assertEqual(
                start, datetime(2020, 1, 31, 12, 0, 0, tzinfo=dt_timezone.utc)
            )
            self.assertEqual(
                end, datetime(2020, 2, 29, 12, 0, 0, tzinfo=dt_timezone.utc)
            )

        # Test edge case: Created Jan 31, Now April 10
        # Cycle should be Mar 31 - Apr 30
        with freeze_time("2020-04-10"):
            start, end = get_free_tier_cycle(created)
            self.assertEqual(
                start, datetime(2020, 3, 31, 12, 0, 0, tzinfo=dt_timezone.utc)
            )
            self.assertEqual(
                end, datetime(2020, 4, 30, 12, 0, 0, tzinfo=dt_timezone.utc)
            )

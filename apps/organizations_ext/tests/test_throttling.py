from datetime import timedelta

from asgiref.sync import async_to_sync
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
)


class OrganizationThrottleCheckTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.product = baker.make("stripe.StripeProduct", events=10)
        cls.price = baker.make("stripe.StripePrice", price=0, product=cls.product)
        cls.organization = baker.make("organizations_ext.Organization")
        cls.user = baker.make("users.user")
        cls.organization.add_user(cls.user)
        cls.subscription = baker.make(
            "stripe.StripeSubscription",
            organization=cls.organization,
            price=cls.price,
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() + timedelta(hours=1),
        )
        cls.organization.stripe_primary_subscription = cls.subscription
        cls.organization.save()

    def _make_events(self, i: int):
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=i,
        )

    def _make_transaction_events(self, i: int):
        baker.make(
            "projects.TransactionEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=i,
        )

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    )
    def test_check_organization_throttle(self):
        check_organization_throttle(self.organization.id)
        self.assertTrue(Organization.objects.filter(event_throttle_rate=0).exists())

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=11,
        )
        check_organization_throttle(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 10)
        self.assertEqual(len(mail.outbox), 1)

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=100,
        )
        check_organization_throttle(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 100)
        self.assertEqual(len(mail.outbox), 2)

    def test_bypass_organization_throttle(self):
        """Ensure bypassing the check org throttle cache works"""
        check_organization_throttle(self.organization.id)
        self.assertTrue(cache.get(f"org-throttle-{self.organization.id}"))
        cache.clear()
        check_organization_throttle(self.organization.id, True)
        self.assertFalse(cache.get(f"org-throttle-{self.organization.id}"))
        cache.clear()

    def test_check_all_organizations_throttle(self):
        org = self.organization

        # No events, no throttle
        with self.assertNumQueries(1):
            check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)

        # 6 events (of 10), no throttle
        self._make_events(3)
        self._make_transaction_events(3)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)
        self.assertEqual(len(mail.outbox), 0)

        # 11 events (of 10), small throttle
        self._make_events(5)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 10)
        self.assertEqual(len(mail.outbox), 1)

        # New time period, should reset throttle
        now = self.subscription.current_period_start + timedelta(hours=1)
        self.subscription.current_period_start = now
        self.subscription.current_period_end = now + timedelta(hours=1)
        self.subscription.save()
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)
        self.assertEqual(len(mail.outbox), 1)

        # Throttle again
        with freeze_time(now):
            self._make_events(16)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 50)

        # Throttle 100%
        with freeze_time(now):
            self._make_events(5)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 100)

    def test_no_plan_throttle(self):
        """
        It's possible to not sign up for a free plan, they should be throttled
        """
        self.subscription.delete()
        check_all_organizations_throttle()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 100)

        # Make plan active
        subscription = baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            price=self.price,
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() + timedelta(hours=1),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        check_all_organizations_throttle()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)

        # Cancel plan
        subscription.status = SubscriptionStatus.CANCELED
        subscription.save()
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        check_all_organizations_throttle()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 100)

        # Add new active plan (still has canceled plan)
        subscription = baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            price=self.price,
            status=SubscriptionStatus.ACTIVE,
            current_period_end=timezone.now() + timedelta(hours=1),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        check_all_organizations_throttle()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 0)

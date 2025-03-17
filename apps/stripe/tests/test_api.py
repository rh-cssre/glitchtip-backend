from datetime import datetime
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription


class StripeAPITestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make(
            "organizations_ext.Organization", stripe_customer_id="cust_1"
        )
        cls.org_user = cls.organization.add_user(cls.user)
        cls.product = baker.make("stripe.StripeProduct", is_public=True, events=5)
        cls.price = baker.make("stripe.StripePrice", product=cls.product, price=0)
        cls.product.default_price = cls.price
        cls.product.save()

    def setUp(self):
        self.client.force_login(self.user)

    def test_list_stripe_products(self):
        url = reverse("api:list_stripe_products")
        res = self.client.get(url)
        self.assertContains(res, self.product.name)

    def test_get_stripe_subscription(self):
        sub = baker.make(
            "stripe.StripeSubscription", organization=self.organization, status=SubscriptionStatus.ACTIVE
        )
        url = reverse("api:get_stripe_subscription", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, sub.stripe_id)

    @patch("apps.stripe.api.create_session")
    def test_create_stripe_session(self, mock_create_session):
        url = reverse("api:create_stripe_session", args=[self.organization.slug])
        mock_create_session.return_value = {"id": "test"}
        res = self.client.post(
            url, {"price": self.price.stripe_id}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

    @patch("apps.stripe.api.create_portal_session", new_callable=AsyncMock)
    def test_manage_billing(self, mock_create_portal_session):
        mock_create_portal_session.return_value = {"id": "test"}
        url = reverse(
            "api:stripe_billing_portal_session", args=[self.organization.slug]
        )
        res = self.client.post(url, {}, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        mock_create_portal_session.assert_called_once()

    @patch("apps.stripe.api.create_subscription")
    def test_stripe_create_subscription(self, mock_create_subscription):
        mock_create_subscription.return_value.id = "test"
        url = reverse("api:stripe_create_subscription")
        res = self.client.post(
            url,
            {"organization": str(self.organization.id), "price": self.price.stripe_id},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        mock_create_subscription.assert_called_once()

    def test_events_count(self):
        # Ensure we don't filter on any unrelated subscription
        baker.make("stripe.StripeSubscription", status=SubscriptionStatus.ACTIVE)
        # Create a few subscriptions, but only one is active
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.CANCELED
        )
        # Active subscription has a set time period to match events
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(datetime(2020, 1, 2)),
            current_period_end=timezone.make_aware(datetime(2020, 2, 2)),
        )
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.CANCELED,
        )
        url = reverse("api:subscription_events_count", args=[self.organization.slug])
        with freeze_time(datetime(2020, 3, 1)):
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=self.organization,
            )
        with freeze_time(datetime(2020, 1, 5)):
            baker.make("issue_events.IssueEvent")
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=self.organization,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=self.organization,
                count=1,
            )
            baker.make(
                "performance.TransactionEvent",
                group__project__organization=self.organization,
            )
            baker.make(
                "projects.TransactionEventProjectHourlyStatistic",
                project__organization=self.organization,
                count=1,
            )
            baker.make(
                "sourcecode.DebugSymbolBundle",
                file__blob__size=1000000,
                organization=self.organization,
                release__organization=self.organization,
                _quantity=2,
            )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        res = self.client.get(url)
        self.assertEqual(
            res.json(),
            {
                "eventCount": 1,
                "fileSizeMB": 2,
                "transactionEventCount": 1,
                "uptimeCheckEventCount": 0,
            },
        )

    def test_events_count_without_customer(self):
        """
        Due to async nature of Stripe integration, a customer may not exist
        """
        baker.make("stripe.StripeSubscription")
        url = reverse("api:subscription_events_count", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertEqual(sum(res.json().values()), 0)

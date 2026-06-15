from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription
from apps.stripe.utils import unix_to_datetime


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

    def test_list_stripe_products_excludes_non_public_prices(self):
        public_price = baker.make(
            "stripe.StripePrice",
            product=self.product,
            price=10,
            is_public=True,
            interval="month",
        )
        private_price = baker.make(
            "stripe.StripePrice",
            product=self.product,
            price=8,
            is_public=False,
            interval="month",
        )
        url = reverse("api:list_stripe_products")
        res = self.client.get(url)
        body = res.content.decode()
        self.assertIn(public_price.stripe_id, body)
        self.assertNotIn(private_price.stripe_id, body)

    def test_get_stripe_subscription(self):
        sub = baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
        )
        url = reverse("api:get_stripe_subscription", args=[self.organization.slug])
        res = self.client.get(url)
        self.assertContains(res, sub.stripe_id)

    @patch("apps.stripe.api.create_session")
    def test_create_stripe_session(self, mock_create_session):
        url = reverse("api:create_stripe_session", args=[self.organization.slug])
        mock_create_session.return_value = {"url": "test"}
        res = self.client.post(
            url, {"price": self.price.stripe_id}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

    @patch("apps.stripe.api.create_portal_session", new_callable=AsyncMock)
    def test_manage_billing(self, mock_create_portal_session):
        mock_create_portal_session.return_value = {"url": "test"}
        url = reverse(
            "api:stripe_billing_portal_session", args=[self.organization.slug]
        )
        res = self.client.post(url, {}, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        mock_create_portal_session.assert_called_once()

    @patch("apps.stripe.api.create_subscription")
    def test_stripe_create_subscription(self, mock_create_subscription):
        period_start = 1681564800
        period_end = 1684243200
        mock_create_subscription.return_value.id = "test"
        mock_create_subscription.return_value.start_date = period_start
        mock_create_subscription.return_value.collection_method = "charge_automatically"
        mock_create_subscription.return_value.created = period_start
        item = MagicMock()
        item.current_period_start = period_start
        item.current_period_end = period_end
        item.price.recurring = {"interval": "month"}
        mock_create_subscription.return_value.items.data = [item]
        url = reverse("api:stripe_create_subscription")
        res = self.client.post(
            url,
            {"organization": str(self.organization.id), "price": self.price.stripe_id},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        mock_create_subscription.assert_called_once()
        sub = StripeSubscription.objects.get(stripe_id="test")
        self.assertEqual(sub.subscription_cycle_start, unix_to_datetime(period_start))
        self.assertEqual(sub.subscription_cycle_end, unix_to_datetime(period_end))


    def test_subscription_events_count_for_period_current(self):
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(datetime(2020, 2, 1)),
            current_period_end=timezone.make_aware(datetime(2020, 3, 1)),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 2, 15, 10)),
            count=10,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        res = self.client.get(url)  # periods_ago=0 default
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["eventCount"], 10)
        self.assertIn("total", data)

    def test_subscription_events_count_for_period_previous(self):
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(datetime(2020, 2, 1)),
            current_period_end=timezone.make_aware(datetime(2020, 3, 1)),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=25,
        )
        baker.make(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=40,
        )
        baker.make(
            "uptime.UptimeCheckHourlyStatistic",
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=50,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        res = self.client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["eventCount"], 25)
        # Logs and uptime checks each weigh 0.1: reported per-category as the
        # billed contribution (count // 10), same as the current-period branch.
        self.assertEqual(data["logEventCount"], 4)
        self.assertEqual(data["uptimeCheckEventCount"], 5)
        # 25 issues + 40 logs * 0.1 + 50 uptime * 0.1 = 25 + 4 + 5
        self.assertEqual(data["total"], 34)

    def test_subscription_events_count_for_period_retention_limit(self):
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        # periods_ago=4 → 120 days, exceeds default retention
        res = self.client.get(url, {"periods_ago": 4})
        self.assertEqual(res.status_code, 400)
        # periods_ago=1 should always pass even with low retention configured
        with self.settings(GLITCHTIP_RETENTION_DAYS=14):
            res = self.client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)

    def test_subscription_events_count_for_period_no_subscription(self):
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        res = self.client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], 0)

    def test_events_count_daily(self):
        project = baker.make("projects.Project", organization=self.organization)
        period_start = timezone.make_aware(datetime(2020, 1, 1))
        period_end = timezone.make_aware(datetime(2020, 2, 1))
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=period_start,
            current_period_end=period_end,
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )
        # Create stats on two different days
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 5, 10)),
            count=15,
        )
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 5, 14)),
            count=5,
        )
        baker.make(
            "projects.TransactionEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 10, 8)),
            count=30,
        )
        baker.make(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 10, 8)),
            count=40,
        )
        baker.make(
            "uptime.UptimeCheckHourlyStatistic",
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 10, 8)),
            count=50,
        )

        url = reverse(
            "api:subscription_events_count_daily",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 1, 15)):
            res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        # Should have entries from Jan 1 through Jan 15 (today)
        self.assertEqual(len(data), 15)
        # Jan 5 should have 20 issue events (15 + 5 from two hourly rows)
        jan5 = next(d for d in data if d["date"] == "2020-01-05")
        self.assertEqual(jan5["eventCount"], 20)
        self.assertEqual(jan5["transactionEventCount"], 0)
        self.assertEqual(jan5["logEventCount"], 0)
        # Jan 10 should have 30 transaction events, 40 logs (=4 events), and
        # 50 uptime checks (=5 events); logs and uptime are both weighted 0.1
        jan10 = next(d for d in data if d["date"] == "2020-01-10")
        self.assertEqual(jan10["eventCount"], 0)
        self.assertEqual(jan10["transactionEventCount"], 30)
        self.assertEqual(jan10["logEventCount"], 4)
        self.assertEqual(jan10["uptimeCheckEventCount"], 5)
        # Jan 1 should be all zeros
        jan1 = data[0]
        self.assertEqual(jan1["date"], "2020-01-01")
        self.assertEqual(jan1["eventCount"], 0)
        self.assertEqual(jan1["transactionEventCount"], 0)
        self.assertEqual(jan1["uptimeCheckEventCount"], 0)
        self.assertEqual(jan1["logEventCount"], 0)

    def test_events_count_daily_no_subscription(self):
        url = reverse(
            "api:subscription_events_count_daily",
            args=[self.organization.slug],
        )
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"], [])

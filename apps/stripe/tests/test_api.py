from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from apps.organizations_ext.models import Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription
from apps.stripe.utils import unix_to_datetime


# These usage endpoints branch on BILLING_ENABLED. It is False by default (and
# in CI), so pin it True here to exercise the SaaS/Stripe paths deterministically;
# self-hosted cases override it back to False per-test.
@override_settings(BILLING_ENABLED=True)
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
        self.async_client.force_login(self.user)

    async def test_list_stripe_products(self):
        url = reverse("api:list_stripe_products")
        res = await self.async_client.get(url)
        self.assertContains(res, self.product.name)

    async def test_list_stripe_products_excludes_non_public_prices(self):
        public_price = await baker.amake(
            "stripe.StripePrice",
            product=self.product,
            price=10,
            is_public=True,
            interval="month",
        )
        private_price = await baker.amake(
            "stripe.StripePrice",
            product=self.product,
            price=8,
            is_public=False,
            interval="month",
        )
        url = reverse("api:list_stripe_products")
        res = await self.async_client.get(url)
        body = res.content.decode()
        self.assertIn(public_price.stripe_id, body)
        self.assertNotIn(private_price.stripe_id, body)

    async def test_get_stripe_subscription(self):
        sub = await baker.amake(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
        )
        url = reverse("api:get_stripe_subscription", args=[self.organization.slug])
        res = await self.async_client.get(url)
        self.assertContains(res, sub.stripe_id)

    @patch("apps.stripe.api.create_session")
    async def test_create_stripe_session(self, mock_create_session):
        url = reverse("api:create_stripe_session", args=[self.organization.slug])
        mock_create_session.return_value = {"url": "test"}
        res = await self.async_client.post(
            url, {"price": self.price.stripe_id}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 200)

    async def test_create_stripe_session_rejects_metered_price(self):
        # The metered overage price is attached via configure_overage, never
        # sold through checkout.
        overage_product = await baker.amake(
            "stripe.StripeProduct", events=0, is_overage=True
        )
        metered_price = await baker.amake(
            "stripe.StripePrice", product=overage_product, price=0, is_metered=True
        )
        url = reverse("api:create_stripe_session", args=[self.organization.slug])
        res = await self.async_client.post(
            url, {"price": metered_price.stripe_id}, content_type="application/json"
        )
        self.assertEqual(res.status_code, 404)

    @patch("apps.stripe.api.create_portal_session", new_callable=AsyncMock)
    async def test_manage_billing(self, mock_create_portal_session):
        mock_create_portal_session.return_value = {"url": "test"}
        url = reverse(
            "api:stripe_billing_portal_session", args=[self.organization.slug]
        )
        res = await self.async_client.post(url, {}, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        mock_create_portal_session.assert_called_once()

    @patch("apps.stripe.api.create_subscription")
    async def test_stripe_create_subscription(self, mock_create_subscription):
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
        res = await self.async_client.post(
            url,
            {"organization": str(self.organization.id), "price": self.price.stripe_id},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        mock_create_subscription.assert_called_once()
        sub = await StripeSubscription.objects.aget(stripe_id="test")
        self.assertEqual(sub.subscription_cycle_start, unix_to_datetime(period_start))
        self.assertEqual(sub.subscription_cycle_end, unix_to_datetime(period_end))

    async def test_stripe_create_subscription_rejects_metered_price(self):
        # The metered overage price also stores price=0 (its cost lives in
        # tiers), but it is not a base plan and must not be selectable here.
        overage_product = await baker.amake(
            "stripe.StripeProduct", events=0, is_overage=True
        )
        metered_price = await baker.amake(
            "stripe.StripePrice", product=overage_product, price=0, is_metered=True
        )
        url = reverse("api:stripe_create_subscription")
        res = await self.async_client.post(
            url,
            {
                "organization": str(self.organization.id),
                "price": metered_price.stripe_id,
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(await StripeSubscription.objects.aexists())

    async def test_subscription_events_count_for_period_current(self):
        project = await baker.amake("projects.Project", organization=self.organization)
        await baker.amake(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(datetime(2020, 2, 1)),
            current_period_end=timezone.make_aware(datetime(2020, 3, 1)),
        )
        await StripeSubscription.set_primary_subscriptions_for_organizations(
            {self.organization.id}
        )
        await baker.amake(
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
        res = await self.async_client.get(url)  # periods_ago=0 default
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["eventCount"], 10)
        self.assertIn("total", data)

    async def test_subscription_events_count_for_period_previous(self):
        project = await baker.amake("projects.Project", organization=self.organization)
        await baker.amake(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(datetime(2020, 2, 1)),
            current_period_end=timezone.make_aware(datetime(2020, 3, 1)),
        )
        await StripeSubscription.set_primary_subscriptions_for_organizations(
            {self.organization.id}
        )
        await baker.amake(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=25,
        )
        await baker.amake(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=40,
        )
        await baker.amake(
            "uptime.UptimeCheckHourlyStatistic",
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 15, 10)),
            count=50,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        res = await self.async_client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["eventCount"], 25)
        # Logs and uptime checks each weigh 0.1: reported per-category as the
        # billed contribution (count // 10), same as the current-period branch.
        self.assertEqual(data["logEventCount"], 4)
        self.assertEqual(data["uptimeCheckEventCount"], 5)
        # 25 issues + 40 logs * 0.1 + 50 uptime * 0.1 = 25 + 4 + 5
        self.assertEqual(data["total"], 34)

    async def test_subscription_events_count_for_period_retention_limit(self):
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        # periods_ago=4 → 120 days, exceeds default retention
        res = await self.async_client.get(url, {"periods_ago": 4})
        self.assertEqual(res.status_code, 400)
        # periods_ago=1 should always pass even with low retention configured
        with self.settings(GLITCHTIP_RETENTION_DAYS=14):
            res = await self.async_client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)

    async def test_subscription_events_count_for_period_no_subscription(self):
        # SaaS org with no active subscription falls back to the free-tier
        # anchored cycle; with no events in the prior window the total is 0.
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        res = await self.async_client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], 0)

    async def test_events_count_daily(self):
        project = await baker.amake("projects.Project", organization=self.organization)
        period_start = timezone.make_aware(datetime(2020, 1, 1))
        period_end = timezone.make_aware(datetime(2020, 2, 1))
        await baker.amake(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=period_start,
            current_period_end=period_end,
        )
        await StripeSubscription.set_primary_subscriptions_for_organizations(
            {self.organization.id}
        )
        # Create stats on two different days
        await baker.amake(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 5, 10)),
            count=15,
        )
        await baker.amake(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 5, 14)),
            count=5,
        )
        await baker.amake(
            "projects.TransactionEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 10, 8)),
            count=30,
        )
        await baker.amake(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 1, 10, 8)),
            count=40,
        )
        await baker.amake(
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
            res = await self.async_client.get(url)
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

    def _make_org_created(self, created: datetime):
        """Org owned by self.user with a deterministic created date."""
        org = baker.make("organizations_ext.Organization")
        org.add_user(self.user)
        Organization.objects.filter(pk=org.pk).update(
            created=timezone.make_aware(created)
        )
        org.refresh_from_db()
        return org

    def test_events_count_daily_no_subscription(self):
        # SaaS org with no subscription: the daily chart spans the free-tier
        # cycle anchored to org.created (not an empty list, not all-time).
        org = self._make_org_created(datetime(2020, 1, 10))
        project = baker.make("projects.Project", organization=org)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=org,
            date=timezone.make_aware(datetime(2020, 2, 12, 9)),
            count=7,
        )
        url = reverse(
            "api:subscription_events_count_daily",
            args=[org.slug],
        )
        with freeze_time(datetime(2020, 2, 15)):
            res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        # Anchored cycle is 2020-02-10 .. 2020-03-10; capped at today (02-15).
        self.assertEqual(len(data), 6)  # Feb 10 .. Feb 15 inclusive
        feb12 = next(d for d in data if d["date"] == "2020-02-12")
        self.assertEqual(feb12["eventCount"], 7)

    def test_period_free_tier_uses_anchored_cycle_without_subscription(self):
        # SaaS org with no subscription: current-period usage comes from the
        # free-tier cycle anchored to org.created, not all-time and not rolling.
        org = self._make_org_created(datetime(2020, 1, 10))
        project = baker.make("projects.Project", organization=org)
        # In the current anchored cycle (2020-02-10 .. 2020-03-10).
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=org,
            date=timezone.make_aware(datetime(2020, 2, 20, 9)),
            count=12,
        )
        # In the previous cycle (2020-01-10 .. 2020-02-10) — must be excluded.
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=org,
            date=timezone.make_aware(datetime(2020, 1, 20, 9)),
            count=99,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[org.slug],
        )
        with freeze_time(datetime(2020, 2, 25)):
            res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["eventCount"], 12)

    def test_period_free_tier_previous_cycle_without_subscription(self):
        # periods_ago=1 for a free-tier org = the prior anchored cycle.
        org = self._make_org_created(datetime(2020, 1, 10))
        project = baker.make("projects.Project", organization=org)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=org,
            date=timezone.make_aware(datetime(2020, 1, 20, 9)),  # prior cycle
            count=8,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[org.slug],
        )
        with freeze_time(datetime(2020, 2, 25)):
            res = self.client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["eventCount"], 8)

    @override_settings(BILLING_ENABLED=False)
    def test_period_self_hosted_uses_rolling_window_without_subscription(self):
        # Self-hosted (BILLING_ENABLED=False): usage is reported over a rolling
        # 30-day window and must NOT require a StripeSubscription row.
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 1, 10)),  # within last 30d
            count=10,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 3, 15)):
            res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["eventCount"], 10)

    @override_settings(BILLING_ENABLED=False)
    def test_period_self_hosted_previous_window_without_subscription(self):
        # "Last 30 days" on self-hosted = the prior rolling window, still no sub.
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 2, 1, 10)),  # in prior window
            count=25,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 3, 15)):
            res = self.client.get(url, {"periods_ago": 1})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["eventCount"], 25)

    @override_settings(BILLING_ENABLED=False)
    def test_events_count_daily_self_hosted_without_subscription(self):
        # Self-hosted daily chart spans the rolling 30-day window, no sub required.
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 10, 8)),
            count=15,
        )
        baker.make(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 10, 8)),
            count=40,
        )
        url = reverse(
            "api:subscription_events_count_daily",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 3, 15)):
            res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        data = res.json()["data"]
        self.assertEqual(len(data), 31)  # 2020-02-14 .. 2020-03-15 inclusive
        mar10 = next(d for d in data if d["date"] == "2020-03-10")
        self.assertEqual(mar10["eventCount"], 15)
        self.assertEqual(mar10["logEventCount"], 4.0)  # 40 * 0.1, unfloored

    def _make_active_subscription(self, period_start: datetime, period_end: datetime):
        baker.make(
            "stripe.StripeSubscription",
            organization=self.organization,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=timezone.make_aware(period_start),
            current_period_end=timezone.make_aware(period_end),
        )
        async_to_sync(StripeSubscription.set_primary_subscriptions_for_organizations)(
            {self.organization.id}
        )

    @override_settings(BILLING_ENABLED=True)
    def test_daily_billed_series_sums_to_period_total_low_volume(self):
        # BUG-006 regression: low-volume uptime (5/day) must NOT floor to 0 on
        # each daily bar, and the daily series must sum to the period total.
        # BILLING_ENABLED so the period endpoint date-bounds to the subscription
        # cycle (it counts all events otherwise), exercising the billed path.
        self._make_active_subscription(datetime(2020, 3, 1), datetime(2020, 4, 1))
        for day in range(10, 16):  # Mar 10..15, six days
            baker.make(
                "uptime.UptimeCheckHourlyStatistic",
                organization=self.organization,
                date=timezone.make_aware(datetime(2020, 3, day, 8)),
                count=5,
            )
        period_url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        daily_url = reverse(
            "api:subscription_events_count_daily",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 3, 16)):
            period = self.client.get(period_url).json()
            daily = self.client.get(daily_url).json()["data"]

        # 6 days * 5 checks = 30 raw uptime = 3.0 billed; old per-day floor gave 0.
        self.assertEqual(period["uptimeCheckEventCount"], 3.0)
        self.assertEqual(period["total"], 3)
        seeded = [d for d in daily if d["uptimeCheckEventCount"]]
        self.assertEqual(len(seeded), 6)  # every seeded day shows up (0.5 each)
        self.assertTrue(all(d["uptimeCheckEventCount"] == 0.5 for d in seeded))
        # The daily series reconciles exactly with the period figure.
        self.assertEqual(
            sum(d["uptimeCheckEventCount"] for d in daily),
            period["uptimeCheckEventCount"],
        )

    @override_settings(BILLING_ENABLED=True)
    def test_breakdown_categories_reconcile_with_total(self):
        # Per-category billed values are unfloored, so they sum to the period
        # total within the single final floor (old code floored each category).
        self._make_active_subscription(datetime(2020, 3, 1), datetime(2020, 4, 1))
        project = baker.make("projects.Project", organization=self.organization)
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 10, 8)),
            count=250,
        )
        baker.make(
            "projects.LogProjectHourlyStatistic",
            project=project,
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 10, 8)),
            count=40,
        )
        baker.make(
            "uptime.UptimeCheckHourlyStatistic",
            organization=self.organization,
            date=timezone.make_aware(datetime(2020, 3, 10, 8)),
            count=55,
        )
        url = reverse(
            "api:subscription_events_count_for_period",
            args=[self.organization.slug],
        )
        with freeze_time(datetime(2020, 3, 15)):
            data = self.client.get(url).json()
        self.assertEqual(data["uptimeCheckEventCount"], 5.5)  # 55 * 0.1, unfloored
        self.assertEqual(data["logEventCount"], 4.0)
        self.assertEqual(data["eventCount"], 250)
        category_sum = (
            data["eventCount"]
            + data["transactionEventCount"]
            + data["uptimeCheckEventCount"]
            + data["logEventCount"]
        )
        # 250 + 0 + 5.5 + 4.0 = 259.5 ; total floored once = 259.
        self.assertEqual(data["total"], 259)
        self.assertLess(abs(category_sum - data["total"]), 1)

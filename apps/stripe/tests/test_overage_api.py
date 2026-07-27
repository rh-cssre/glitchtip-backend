from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.exceptions import StripeError
from apps.stripe.models import StripeSubscription

QUOTA = 100_000


@override_settings(
    BILLING_ENABLED=True,
    GLITCHTIP_OVERAGE_TIERS=[
        (400_000, "0.00015"),
        (2_000_000, "0.00010"),
        (None, "0.00008"),
    ],
)
class OverageAPITestCase(TestCase):
    def setUp(self):
        self.user = baker.make("users.user")
        self.org = baker.make(
            "organizations_ext.Organization", stripe_customer_id="cus_1"
        )
        self.org.add_user(self.user, OrganizationUserRole.OWNER)
        self.client.force_login(self.user)

        now = timezone.now()
        self.product = baker.make("stripe.StripeProduct", events=QUOTA)
        self.price = baker.make("stripe.StripePrice", product=self.product, price=15)
        self.sub = StripeSubscription.objects.create(
            stripe_id="sub_1",
            created=now,
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            start_date=now,
            price=self.price,
            organization=self.org,
            status=SubscriptionStatus.ACTIVE,
        )
        self.org.stripe_primary_subscription = self.sub
        self.org.save(update_fields=["stripe_primary_subscription"])

        # A provisioned metered overage price.
        overage_product = baker.make("stripe.StripeProduct", events=0, is_overage=True)
        self.overage_price = baker.make(
            "stripe.StripePrice", product=overage_product, price=0, is_metered=True
        )

    def _status(self):
        url = reverse("api:get_overage_status", args=[self.org.slug])
        return self.client.get(url)

    def _configure(self, payload):
        url = reverse("api:configure_overage", args=[self.org.slug])
        return self.client.post(url, payload, content_type="application/json")

    def test_status_defaults_disabled(self):
        res = self._status()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertFalse(body["enabled"])
        self.assertTrue(body["eligible"])
        self.assertTrue(body["configured"])
        self.assertEqual(body["quota"], QUOTA)

    def test_enable_attaches_item_and_sets_cap(self):
        item = AsyncMock(return_value=type("I", (), {"id": "si_new"})())
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, None)),
            ),
            patch("apps.stripe.api.add_subscription_item", new=item),
            patch(
                "apps.stripe.api.migrate_subscription_to_flexible",
                new_callable=AsyncMock,
            ),
            patch(
                "apps.stripe.api.check_organization_throttle",
                new=MagicMock(aenqueue=AsyncMock()),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        self.org.refresh_from_db()
        self.sub.refresh_from_db()
        self.assertTrue(self.org.metered_billing_enabled)
        self.assertEqual(self.org.overage_spend_cap_cents, 2000)
        self.assertEqual(self.sub.metered_item_id, "si_new")
        item.assert_awaited_once_with(self.sub.stripe_id, self.overage_price.stripe_id)

    def test_enable_reuses_existing_stripe_item(self):
        # An item orphaned by a prior crash (before we saved its id) is reused
        # rather than re-added, which Stripe would reject as a duplicate.
        existing = type("I", (), {"id": "si_orphan"})()
        add = AsyncMock()
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, existing)),
            ),
            patch("apps.stripe.api.add_subscription_item", new=add),
            patch(
                "apps.stripe.api.migrate_subscription_to_flexible",
                new_callable=AsyncMock,
            ),
            patch(
                "apps.stripe.api.check_organization_throttle",
                new=MagicMock(aenqueue=AsyncMock()),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.metered_item_id, "si_orphan")
        add.assert_not_awaited()

    def test_enable_card_error_returns_402(self):
        # A declined card surfaces Stripe's message to the owner, not a 500.
        declined = StripeError("Your card was declined.", status=402, type="card_error")
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, None)),
            ),
            patch(
                "apps.stripe.api.migrate_subscription_to_flexible",
                new=AsyncMock(side_effect=declined),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 402)
        self.assertEqual(res.json()["detail"], "Your card was declined.")
        self.org.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)

    def test_enable_other_stripe_error_is_generic_502(self):
        # Backend Stripe errors must not leak their detail to the owner.
        boom = StripeError("internal detail", status=500, type="api_error")
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, None)),
            ),
            patch(
                "apps.stripe.api.migrate_subscription_to_flexible",
                new=AsyncMock(side_effect=boom),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 502)
        self.assertNotIn("internal detail", res.json()["detail"])
        self.org.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)

    def test_enable_requires_positive_cap(self):
        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": True, "capCents": 0})
        self.assertEqual(res.status_code, 400)
        self.org.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)

    @override_settings(GLITCHTIP_OVERAGE_MAX_CAP_CENTS=100_000)
    def test_enable_rejects_cap_above_max(self):
        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": True, "capCents": 200_000})
        self.assertEqual(res.status_code, 400)
        self.org.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)

    def test_disable_keeps_item_and_counter(self):
        # Disabling must not detach the item or reset the counter: the meter is
        # cumulative per customer for the cycle, so a re-enable would otherwise
        # re-bill already-reported usage (the mid-cycle double-charge bug).
        self.sub.metered_item_id = "si_old"
        self.sub.overage_units_reported = 1234
        self.sub.save()
        self.org.metered_billing_enabled = True
        self.org.save()

        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": False})
        self.assertEqual(res.status_code, 200)
        self.org.refresh_from_db()
        self.sub.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)
        self.assertEqual(self.sub.metered_item_id, "si_old")
        self.assertEqual(self.sub.overage_units_reported, 1234)

    def test_non_owner_cannot_configure(self):
        member = baker.make("users.user")
        self.org.add_user(member, OrganizationUserRole.MEMBER)
        self.client.force_login(member)
        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 404)

    def test_status_reports_overage_cost_when_enabled(self):
        from apps.organizations_ext.models import EventCounts

        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()

        # 150k usage = 50k over the 100k quota; 50,000 * $0.00015 = $7.50.
        with patch(
            "apps.stripe.api.get_event_counts",
            new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
        ):
            body = self._status().json()
        self.assertEqual(body["overageUnits"], 50_000)
        self.assertEqual(body["overageCostCents"], 750)

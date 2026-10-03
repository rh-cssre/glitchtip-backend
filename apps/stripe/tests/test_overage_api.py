from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from apps.organizations_ext.models import EventCounts
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.exceptions import StripeError
from apps.stripe.models import StripeSubscription
from apps.stripe.overage import units_for_budget

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
        self.overage_product = baker.make(
            "stripe.StripeProduct", events=None, is_overage=True
        )
        self.overage_price = baker.make(
            "stripe.StripePrice", product=self.overage_product, price=0, is_metered=True
        )
        # Enabling first checks the customer's other Stripe subscriptions for a
        # metered item; default to "none" so each test only stubs what it's about.
        self.other_subscriptions = AsyncMock(return_value=[])
        patcher = patch(
            "apps.stripe.api.list_customer_subscriptions", new=self.other_subscriptions
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stripe_subscription(self, id, metered=False):
        """A fetched Stripe subscription with a base item and maybe a metered one."""
        base = MagicMock()
        base.price.recurring = {"usage_type": "licensed"}
        items = [base]
        if metered:
            items.append(self._item("si_other"))
        sub = MagicMock(id=id)
        sub.items.data = items
        return sub

    def _item(self, id, period=None):
        """A metered subscription item as Stripe returns it, with its period."""
        now = timezone.now().replace(microsecond=0)
        start, end = period or (now - timedelta(days=1), now + timedelta(days=29))
        item = MagicMock(id=id)
        item.price.recurring = {"usage_type": "metered"}
        item.current_period_start = int(start.timestamp())
        item.current_period_end = int(end.timestamp())
        return item

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
        self.assertFalse(body["active"])
        self.assertEqual(body["quota"], QUOTA)

    def test_status_serves_the_configured_cap_ceiling(self):
        """The client validates a cap against this before posting it, so it has
        to be the same number configure_overage enforces."""
        self.assertEqual(
            self._status().json()["maxCapCents"],
            settings.GLITCHTIP_OVERAGE_MAX_CAP_CENTS,
        )

    @override_settings(GLITCHTIP_OVERAGE_MAX_CAP_CENTS=5_000_000)
    def test_status_follows_a_raised_ceiling(self):
        # Deployments raise this for large customers; a hardcoded client-side
        # maximum would reject caps the server would accept.
        body = self._status().json()
        self.assertEqual(body["maxCapCents"], 5_000_000)
        res = self._configure({"enabled": True, "capCents": 2_000_000})
        self.assertNotEqual(res.status_code, 400)

    def test_active_mirrors_the_throttle_predicate(self):
        """The metered item can vanish while enabled and eligible stay set."""
        self.org.metered_billing_enabled = True
        self.org.save(update_fields=["metered_billing_enabled"])
        self.sub.metered_item_id = "si_1"
        self.sub.save(update_fields=["metered_item_id"])
        self.assertTrue(self._status().json()["active"])

        self.sub.metered_item_id = ""
        self.sub.save(update_fields=["metered_item_id"])
        body = self._status().json()
        self.assertTrue(body["enabled"])
        self.assertTrue(body["eligible"])
        self.assertFalse(body["active"])

    def test_active_false_when_plan_ineligible(self):
        self.org.metered_billing_enabled = True
        self.org.save(update_fields=["metered_billing_enabled"])
        self.sub.metered_item_id = "si_1"
        self.sub.save(update_fields=["metered_item_id"])
        self.price.price = 0
        self.price.save(update_fields=["price"])

        body = self._status().json()
        self.assertTrue(body["enabled"])
        self.assertFalse(body["eligible"])
        self.assertFalse(body["active"])

    def test_archived_overage_product_is_not_configured(self):
        """Archiving the overage product in Stripe turns the feature off — this
        lookup is the only gate, and the frontend hides the card on it."""
        self.overage_product.active = False
        self.overage_product.save(update_fields=["active"])

        self.assertFalse(self._status().json()["configured"])

    def test_archived_overage_price_is_not_configured(self):
        """Same for retiring just the price (e.g. a superseded tier schedule)."""
        self.overage_price.active = False
        self.overage_price.save(update_fields=["active"])

        self.assertFalse(self._status().json()["configured"])

    def test_quotaless_plan_is_not_eligible(self):
        """`eligible` must agree with the guard configure_overage applies, or the
        card offers a toggle the POST would reject."""
        self.product.events = None
        self.product.save(update_fields=["events"])

        body = self._status().json()
        self.assertFalse(body["eligible"])

        res = self._configure({"enabled": True, "capCents": 5000})
        self.assertEqual(res.status_code, 400)

    def test_free_plan_is_not_eligible(self):
        """Overage layers a usage charge on a paid plan, so a $0 plan must not
        take it: self-serve signup only creates free subscriptions, so a check
        for "has a subscription" would let any free org attach a metered item
        (and be invoiced) with no card on file."""
        self.price.price = 0
        self.price.save(update_fields=["price"])

        body = self._status().json()
        self.assertFalse(body["eligible"])
        # Eligibility and quota are separate questions: the plan still has its
        # quota and the throttle path still enforces it, so the card must keep
        # reporting the real number rather than falling back to the free tier.
        self.assertEqual(body["quota"], QUOTA)

        res = self._configure({"enabled": True, "capCents": 5000})
        self.assertEqual(res.status_code, 400)

    def test_inactive_subscription_is_not_eligible(self):
        """The primary-subscription pointer is cleared asynchronously, so a
        canceled subscription can still be attached when the request lands."""
        self.sub.status = SubscriptionStatus.CANCELED
        self.sub.save(update_fields=["status"])

        self.assertFalse(self._status().json()["eligible"])

        res = self._configure({"enabled": True, "capCents": 5000})
        self.assertEqual(res.status_code, 400)

    def test_recoverable_statuses_stay_eligible(self):
        """Eligibility tracks the active set, not "not canceled". trialing and
        past_due are live subscriptions whose owners must keep the control."""
        for status, expected in (
            (SubscriptionStatus.TRIALING, True),
            (SubscriptionStatus.PAST_DUE, True),
            (SubscriptionStatus.UNPAID, False),
            (SubscriptionStatus.PAUSED, False),
            (SubscriptionStatus.CANCELED, False),
        ):
            with self.subTest(status):
                self.sub.status = status
                self.sub.save(update_fields=["status"])
                self.assertIs(self._status().json()["eligible"], expected)

    def test_enable_attaches_item_and_sets_cap(self):
        now = timezone.now().replace(microsecond=0)
        period = (now - timedelta(days=1), now + timedelta(days=29))
        item = AsyncMock(return_value=self._item("si_new", period))
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, None)),
            ),
            patch("apps.stripe.api.add_subscription_item", new=item),
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
        # The item's own Stripe period is the window from here on; recorded at
        # once so the re-check enqueued by this request bills against it.
        self.assertEqual(self.sub.metered_period_start, period[0])
        self.assertEqual(self.sub.metered_period_end, period[1])
        item.assert_awaited_once_with(self.sub.stripe_id, self.overage_price.stripe_id)

    def test_enable_reuses_existing_stripe_item(self):
        # An item orphaned by a prior crash (before we saved its id) is reused
        # rather than re-added, which Stripe would reject as a duplicate.
        now = timezone.now().replace(microsecond=0)
        period = (now - timedelta(days=1), now + timedelta(days=29))
        existing = self._item("si_orphan", period)
        add = AsyncMock()
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch(
                "apps.stripe.api.select_subscription_items",
                new=MagicMock(return_value=(None, existing)),
            ),
            patch("apps.stripe.api.add_subscription_item", new=add),
            patch(
                "apps.stripe.api.check_organization_throttle",
                new=MagicMock(aenqueue=AsyncMock()),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.metered_item_id, "si_orphan")
        self.assertEqual(self.sub.metered_period_start, period[0])
        self.assertEqual(self.sub.metered_period_end, period[1])
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
                "apps.stripe.api.add_subscription_item",
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
                "apps.stripe.api.add_subscription_item",
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

    def test_disable_keeps_item_and_last_report(self):
        # Disabling only clears the flag. The item stays attached so a re-enable
        # is a flag flip, and the last-report record stays so the in-cycle floor
        # still holds: usage billed while enabled isn't retracted by toggling.
        self.sub.metered_item_id = "si_old"
        self.sub.save()
        self.org.metered_billing_enabled = True
        self.org.overage_last_reported_units = 1234
        self.org.overage_last_reported_at = timezone.now()
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
        self.assertEqual(self.org.overage_last_reported_units, 1234)
        self.assertIsNotNone(self.org.overage_last_reported_at)

    def test_enable_refused_when_another_subscription_is_metered(self):
        """Two metered items on one customer both bill the whole meter.

        Stripe aggregates the meter per customer, not per subscription, so a
        second subscription carrying the metered price double-bills every unit.
        Enabling is refused, and nothing is attached, until the account is
        consolidated.
        """
        self.other_subscriptions.return_value = [
            self._stripe_subscription("sub_1"),
            self._stripe_subscription("sub_other", metered=True),
        ]
        add = AsyncMock()
        with (
            patch("apps.stripe.api.fetch_subscription", new_callable=AsyncMock),
            patch("apps.stripe.api.add_subscription_item", new=add),
            patch(
                "apps.stripe.api.check_organization_throttle",
                new=MagicMock(aenqueue=AsyncMock()),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Another subscription", res.json()["detail"])
        add.assert_not_awaited()
        self.org.refresh_from_db()
        self.assertFalse(self.org.metered_billing_enabled)

    def test_enable_skips_the_orgs_own_metered_subscription(self):
        # The org's own subscription carrying the item is the normal re-enable
        # state, not a second subscription.
        self.sub.metered_item_id = "si_old"
        self.sub.save()
        self.other_subscriptions.return_value = [
            self._stripe_subscription("sub_1", metered=True),
            self._stripe_subscription("sub_other"),
        ]
        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        self.other_subscriptions.assert_awaited_once()
        self.org.refresh_from_db()
        self.assertTrue(self.org.metered_billing_enabled)

    def test_enable_requires_a_billing_account(self):
        self.org.stripe_customer_id = ""
        self.org.save()
        res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 400)
        self.other_subscriptions.assert_not_awaited()

    def test_cap_decrease_is_not_refused_by_another_metered_subscription(self):
        # Lowering the cap only reduces exposure; an account already in the
        # two-metered-subscriptions state must still be able to do it.
        self.sub.metered_item_id = "si_old"
        self.sub.save()
        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()
        self.other_subscriptions.return_value = [
            self._stripe_subscription("sub_other", metered=True)
        ]
        with patch(
            "apps.stripe.api.check_organization_throttle",
            new=MagicMock(aenqueue=AsyncMock()),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        self.other_subscriptions.assert_not_awaited()
        self.org.refresh_from_db()
        self.assertEqual(self.org.overage_spend_cap_cents, 2000)

    def test_cap_decrease_without_an_item_is_still_guarded(self):
        # The bypass is for a cap change on an item that already exists. With
        # no item on the primary subscription the request would attach one,
        # which is exactly what the other-subscription check exists to stop:
        # here the old subscription still carries the metered item.
        self.sub.metered_item_id = ""
        self.sub.save()
        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()
        self.other_subscriptions.return_value = [
            self._stripe_subscription("sub_other", metered=True)
        ]
        with (
            patch(
                "apps.stripe.api.fetch_subscription", new_callable=AsyncMock
            ) as fetch,
            patch(
                "apps.stripe.api.add_subscription_item", new_callable=AsyncMock
            ) as add,
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 400)
        self.other_subscriptions.assert_awaited_once()
        fetch.assert_not_awaited()
        add.assert_not_awaited()
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.metered_item_id, "")

    def test_enable_ignores_other_subscriptions_without_metered_item(self):
        self.other_subscriptions.return_value = [
            self._stripe_subscription("sub_1"),
            self._stripe_subscription("sub_other"),
        ]
        fetched = self._stripe_subscription("sub_1")
        item = AsyncMock(return_value=self._item("si_new"))
        with (
            patch(
                "apps.stripe.api.fetch_subscription",
                new=AsyncMock(return_value=fetched),
            ),
            patch("apps.stripe.api.add_subscription_item", new=item),
            patch(
                "apps.stripe.api.check_organization_throttle",
                new=MagicMock(aenqueue=AsyncMock()),
            ),
        ):
            res = self._configure({"enabled": True, "capCents": 2000})
        self.assertEqual(res.status_code, 200)
        item.assert_awaited_once()

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

    def test_status_shows_the_cycle_quota_and_the_cap_floor(self):
        """What the card shows must be what the sweep bills: the cycle's
        quota high-water mark after a downgrade, and the cap floored at what
        the cycle has already billed after the cap is lowered."""
        now = timezone.now()
        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 500  # ~3,333 affordable units
        self.org.overage_cycle_start = self.sub.current_period_start
        self.org.overage_cycle_quota = 150_000  # the plan before a downgrade
        self.org.overage_last_reported_units = 40_000  # billed before the cap drop
        self.org.overage_last_reported_at = now - timedelta(hours=1)
        self.org.save()
        self.sub.metered_item_id = "si_1"
        self.sub.save(update_fields=["metered_item_id"])

        with patch(
            "apps.stripe.api.get_event_counts",
            new=AsyncMock(return_value=EventCounts(issue_event_count=200_000)),
        ):
            body = self._status().json()
        self.assertEqual(body["quota"], 150_000)
        self.assertEqual(body["overageUnits"], 50_000)
        # 40,000 billed units at $0.00015 = $6.00, not the $5 cap.
        self.assertEqual(body["overageCostCents"], 600)

    def test_status_counts_usage_in_the_metered_items_period(self):
        """What the card shows is counted over the window the sweep bills in:
        the metered item's own Stripe period once one is attached."""
        # Whole seconds, as Stripe's timestamps are (the JSON serializer keeps
        # milliseconds only).
        now = timezone.now().replace(microsecond=0)
        item_period = (now - timedelta(days=2), now + timedelta(days=28))
        self.sub.metered_item_id = "si_1"
        self.sub.metered_period_start, self.sub.metered_period_end = item_period
        self.sub.save()
        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()

        with patch(
            "apps.stripe.api.get_event_counts",
            new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
        ) as counts:
            body = self._status().json()
        self.assertEqual(body["overageUnits"], 50_000)
        self.assertEqual(counts.await_args.args[1:], item_period)

        # And the subscription's cycle, as the billing page shows it.
        res = self.client.get(
            reverse("api:get_stripe_subscription", args=[self.org.slug])
        )
        self.assertEqual(
            datetime.fromisoformat(res.json()["subscriptionCycleStart"]),
            item_period[0],
        )

    def test_status_reports_overage_cost_when_enabled(self):
        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()
        self.sub.metered_item_id = "si_1"
        self.sub.save(update_fields=["metered_item_id"])

        # 150k usage = 50k over the 100k quota; 50,000 * $0.00015 = $7.50.
        with patch(
            "apps.stripe.api.get_event_counts",
            new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
        ):
            body = self._status().json()
        self.assertEqual(body["overageUnits"], 50_000)
        self.assertEqual(body["overageCostCents"], 750)

    def test_no_projected_cost_when_overage_is_inactive(self):
        """A vanished metered item leaves `enabled` set, but the task meters
        nothing — so the card must not project a charge no invoice will carry."""
        from apps.organizations_ext.models import EventCounts

        self.org.metered_billing_enabled = True
        self.org.overage_spend_cap_cents = 5000
        self.org.save()  # no metered_item_id: enabled and eligible, not active

        with patch(
            "apps.stripe.api.get_event_counts",
            new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
        ):
            body = self._status().json()
        self.assertFalse(body["active"])
        # The measurement stands; only the money is gated.
        self.assertEqual(body["overageUnits"], 50_000)
        self.assertEqual(body["capUnits"], units_for_budget(5000))
        self.assertEqual(body["overageCostCents"], 0)

    def test_status_reports_what_the_cycle_has_already_sent(self):
        """Switching overage off stops reporting, not the invoice for the
        figure already sent, so the card is told that figure for as long as
        the item that bills it is attached."""
        now = timezone.now()
        in_cycle = now - timedelta(hours=1)
        last_cycle = self.sub.current_period_start - timedelta(days=1)
        cases = [
            # (name, enabled, item, plan_price, last_at, reported, projected)
            ("never enabled", False, "", 15, None, 0, 0),
            ("disabled this cycle", False, "si_1", 15, in_cycle, 600, 0),
            ("disabled last cycle", False, "si_1", 15, last_cycle, 0, 0),
            ("item gone", False, "", 15, in_cycle, 0, 0),
            ("plan ineligible, flag kept", True, "si_1", 0, in_cycle, 600, 0),
            ("re-enabled", True, "si_1", 15, in_cycle, 600, 750),
        ]
        for name, enabled, item, plan_price, last_at, reported, projected in cases:
            with self.subTest(name):
                self.org.metered_billing_enabled = enabled
                self.org.overage_spend_cap_cents = 5000
                # 40,000 units at $0.00015 = $6.00.
                self.org.overage_last_reported_units = 40_000 if last_at else 0
                self.org.overage_last_reported_at = last_at
                self.org.save()
                self.sub.metered_item_id = item
                self.sub.save(update_fields=["metered_item_id"])
                self.price.price = plan_price
                self.price.save(update_fields=["price"])

                with patch(
                    "apps.stripe.api.get_event_counts",
                    new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
                ):
                    body = self._status().json()
                self.assertEqual(body["reportedCostCents"], reported)
                self.assertEqual(body["overageCostCents"], projected)

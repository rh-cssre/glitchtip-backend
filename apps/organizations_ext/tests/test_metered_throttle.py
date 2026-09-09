from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.models import EventCounts, Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.exceptions import StripeError
from apps.stripe.models import StripeSubscription

from ..tasks import check_organization_throttle

_check = async_to_sync(check_organization_throttle.func)

QUOTA = 100_000
# $20 cap at $0.00015/event (first tier) -> 133,333 affordable overage units.
CAP_CENTS = 2000
CAP_UNITS = 133_333
ALLOWED = QUOTA + CAP_UNITS  # 233,333


def _meter(formula="last"):
    meter = MagicMock(id="mtr_1", event_name="glitchtip_overage_events_total")
    meter.default_aggregation.formula = formula
    return meter


@override_settings(
    BILLING_ENABLED=True,
    GLITCHTIP_OVERAGE_TIERS=[
        (400_000, "0.00015"),
        (2_000_000, "0.00010"),
        (None, "0.00008"),
    ],
)
class MeteredThrottleTestCase(TestCase):
    """The throttle sweep's overage reports.

    The meter aggregates by ``last``, so every report is the cycle-to-date
    billable total and the only local state is a record of the last one sent.
    These tests pin the properties that make that safe: re-sends restate the
    same figure, the figure never goes down within a cycle, out-of-order and
    same-second measurements are skipped, a stale check from a closed cycle is
    dropped, and nothing is sent to a meter that doesn't aggregate by ``last``.
    """

    def setUp(self):
        cache.clear()  # the per-customer lock and the cached meter formula
        # Stripe timestamps are whole seconds, so each run measures 10s later.
        self.clock = timezone.now().replace(microsecond=0)

    def _make_org(
        self,
        *,
        enabled=True,
        cap_cents=CAP_CENTS,
        metered_item="si_1",
        plan_price=15,
        status=SubscriptionStatus.ACTIVE,
        stripe_id="sub_1",
    ):
        now = timezone.now()
        org = baker.make(
            "organizations_ext.Organization",
            stripe_customer_id="cus_1",
            metered_billing_enabled=enabled,
            overage_spend_cap_cents=cap_cents,
        )
        product = baker.make("stripe.StripeProduct", events=QUOTA)
        price = baker.make(
            "stripe.StripePrice", product=product, price=plan_price, no_throttle=False
        )
        sub = StripeSubscription.objects.create(
            stripe_id=stripe_id,
            created=now,
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            start_date=now,
            price=price,
            organization=org,
            status=status,
            metered_item_id=metered_item,
        )
        org.stripe_primary_subscription = sub
        org.save(update_fields=["stripe_primary_subscription"])
        return org, sub

    def _run(self, org, usage, meter_error=None, at=None, formula="last"):
        """Run the throttle check with usage and clock pinned; return the meter mock."""
        if at is None:
            self.clock += timedelta(seconds=10)
            at = self.clock
        with (
            patch(
                "apps.organizations_ext.tasks.get_event_counts",
                new=AsyncMock(return_value=EventCounts(issue_event_count=usage)),
            ),
            patch("apps.organizations_ext.tasks.timezone.now", return_value=at),
            patch(
                "apps.stripe.client.find_meter",
                new=AsyncMock(return_value=_meter(formula)),
            ),
            patch(
                "apps.stripe.client.create_meter_event",
                new=AsyncMock(side_effect=meter_error),
            ) as mock_meter,
        ):
            _check(org.id, bypass_cache=True)
        return mock_meter

    def _throttle(self, org):
        return Organization.objects.get(id=org.id).event_throttle_rate

    def _sent(self, mock_meter):
        return mock_meter.await_args.args[2]

    # -- throttle ramp --------------------------------------------------------

    def test_under_quota_no_throttle_no_report(self):
        org, _ = self._make_org()
        mock_meter = self._run(org, 50_000)
        self.assertEqual(self._throttle(org), 0)
        mock_meter.assert_not_awaited()

    def test_within_cap_reports_total_no_throttle(self):
        org, _ = self._make_org()
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._throttle(org), 0)
        mock_meter.assert_awaited_once()
        self.assertEqual(self._sent(mock_meter), 50_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)
        self.assertEqual(org.overage_last_reported_at, self.clock)

    def test_just_past_cap_ramps_to_ten(self):
        org, _ = self._make_org()
        mock_meter = self._run(org, ALLOWED + 1)
        self.assertEqual(self._throttle(org), 10)
        # Billable is capped at the budget; never report beyond CAP_UNITS.
        self.assertEqual(self._sent(mock_meter), CAP_UNITS)

    def test_ramp_fifty(self):
        org, _ = self._make_org()
        self._run(org, ALLOWED + QUOTA // 2 + 1)
        self.assertEqual(self._throttle(org), 50)

    def test_ramp_hundred_block(self):
        org, _ = self._make_org()
        self._run(org, ALLOWED + QUOTA + 1)
        self.assertEqual(self._throttle(org), 100)

    # -- what gets sent -------------------------------------------------------

    def test_report_carries_measurement_time_and_unique_identifier(self):
        # Stripe files the event by timestamp and dedupes by identifier, so the
        # timestamp is the measurement time and the identifier is unique per send.
        org, _ = self._make_org()
        mock_meter = self._run(org, 150_000)
        seconds = int(self.clock.timestamp())
        self.assertEqual(mock_meter.await_args.args[3], f"cus_1:{seconds}")
        self.assertEqual(mock_meter.await_args.kwargs["timestamp"], seconds)

    def test_growth_reports_the_new_total_not_a_delta(self):
        org, _ = self._make_org()
        self._run(org, 150_000)
        mock_meter = self._run(org, 180_000)
        self.assertEqual(self._sent(mock_meter), 80_000)

    def test_unchanged_usage_reports_nothing(self):
        org, _ = self._make_org()
        self._run(org, 150_000)
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()

    def test_replacement_subscription_restates_the_same_total(self):
        """A second subscription becoming primary must not re-bill the cycle.

        Under the old delta counter a replacement started from zero and re-sent
        everything already reported. Now the figure is the customer's
        cycle-to-date total regardless of which subscription is primary, and
        Stripe keeps the last one, so even an anchor shifted by seconds (the
        checkout-race shape) changes nothing.
        """
        org, sub_a = self._make_org()
        first = self._run(org, 150_000)
        self.assertEqual(self._sent(first), 50_000)

        now = timezone.now()
        sub_b = StripeSubscription.objects.create(
            stripe_id="sub_2",
            created=now + timedelta(minutes=1),
            current_period_start=sub_a.current_period_start + timedelta(seconds=5),
            current_period_end=sub_a.current_period_end + timedelta(seconds=5),
            start_date=now,
            price=sub_a.price,
            organization=org,
            status=SubscriptionStatus.ACTIVE,
            metered_item_id="si_2",
        )
        org.stripe_primary_subscription = sub_b
        org.save(update_fields=["stripe_primary_subscription"])

        second = self._run(org, 150_000)
        second.assert_not_awaited()
        third = self._run(org, 160_000)
        self.assertEqual(self._sent(third), 60_000)

    def test_cycle_rollover_starts_the_total_over(self):
        org, _ = self._make_org()
        org.overage_last_reported_at = timezone.now() - timedelta(days=90)
        org.overage_last_reported_units = 999_999
        org.save(
            update_fields=["overage_last_reported_at", "overage_last_reported_units"]
        )

        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)

    def test_rollover_under_quota_reports_nothing(self):
        # A new cycle with nothing billable sends nothing; Stripe bills the
        # period at zero. There is no anchor to move any more.
        org, _ = self._make_org()
        org.overage_last_reported_at = timezone.now() - timedelta(days=90)
        org.overage_last_reported_units = 50_000
        org.save(
            update_fields=["overage_last_reported_at", "overage_last_reported_units"]
        )
        mock_meter = self._run(org, 50_000)
        mock_meter.assert_not_awaited()

    def test_disable_then_reenable_same_cycle_restates_total(self):
        org, _ = self._make_org()
        self._run(org, 150_000)

        org.metered_billing_enabled = False
        org.save(update_fields=["metered_billing_enabled"])
        self._run(org, 170_000).assert_not_awaited()

        org.metered_billing_enabled = True
        org.save(update_fields=["metered_billing_enabled"])
        mock_meter = self._run(org, 180_000)
        self.assertEqual(self._sent(mock_meter), 80_000)

    # -- the in-cycle floor ----------------------------------------------------

    def test_lowered_cap_stops_growth_but_keeps_billed_usage(self):
        # Dropping the cap after usage was reported must not shrink the figure:
        # Stripe would bill the lower "last" value and forgive the month.
        org, _ = self._make_org()
        self._run(org, 150_000)  # 50,000 reported

        org.overage_spend_cap_cents = 500  # ~3,333 affordable units
        org.save(update_fields=["overage_spend_cap_cents"])
        self._run(org, 160_000).assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)

        org.overage_spend_cap_cents = CAP_CENTS
        org.save(update_fields=["overage_spend_cap_cents"])
        mock_meter = self._run(org, 160_000)
        self.assertEqual(self._sent(mock_meter), 60_000)

    # -- ordering guards -------------------------------------------------------

    def test_same_second_measurement_is_skipped(self):
        # Two events in one second have no defined order at Stripe.
        org, _ = self._make_org()
        self._run(org, 150_000)
        mock_meter = self._run(org, 180_000, at=self.clock)
        mock_meter.assert_not_awaited()

    def test_older_measurement_cannot_overwrite_a_newer_report(self):
        # A sweep that lost the lock race and measured earlier carries a figure
        # that is already superseded; sending it would file an older, lower
        # value with an older timestamp (harmless at Stripe) but must not move
        # the local record backwards.
        org, _ = self._make_org()
        self._run(org, 150_000)
        mock_meter = self._run(org, 180_000, at=self.clock - timedelta(seconds=5))
        mock_meter.assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)

    def test_stale_local_period_after_renewal_is_dropped(self):
        """The overcharge the old counter never had.

        Stripe rolled the period but the webhook hasn't landed, so the local
        dates still describe the old cycle. The figure is the old cycle's total,
        but its timestamp is in the new one: Stripe would file it there and the
        in-cycle floor would then hold it for the whole month -- the prior cycle
        billed twice. A measurement past the local period end is dropped.
        """
        org, sub = self._make_org()
        self._run(org, 150_000)  # 50,000 reported in the current cycle
        StripeSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=timezone.now() - timedelta(days=40),
            current_period_end=timezone.now() - timedelta(minutes=1),
        )
        org.refresh_from_db()
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()
        mock_meter = self._run(org, 999_999)
        mock_meter.assert_not_awaited()

    def test_floor_does_not_carry_across_a_stale_boundary(self):
        # A last report from a cycle the local dates no longer cover is not a
        # floor for the new cycle; the new cycle's own total is sent.
        org, _ = self._make_org()
        org.overage_last_reported_at = timezone.now() - timedelta(days=40)
        org.overage_last_reported_units = 999_999
        org.save(
            update_fields=["overage_last_reported_at", "overage_last_reported_units"]
        )
        mock_meter = self._run(org, 120_000)
        self.assertEqual(self._sent(mock_meter), 20_000)

    def test_stale_check_from_a_closed_cycle_is_dropped(self):
        # The period dates advanced under this check; its timestamp would file
        # the event in the closed period, where a late arrival is billed on the
        # next invoice as a fresh total.
        org, sub = self._make_org()
        StripeSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=timezone.now() + timedelta(days=1),
            current_period_end=timezone.now() + timedelta(days=31),
        )
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()
        org.refresh_from_db()
        self.assertIsNone(org.overage_last_reported_at)

    # -- failure handling ------------------------------------------------------

    def test_failed_send_records_nothing_and_next_sweep_restates(self):
        org, _ = self._make_org()
        self._run(org, 150_000, meter_error=StripeError("", status=503))
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 0)
        self.assertIsNone(org.overage_last_reported_at)

        retry = self._run(org, 150_000)
        self.assertEqual(self._sent(retry), 50_000)

    def test_rejected_send_records_nothing(self):
        # A 400 (a duplicate identifier, a bad timestamp) is not retried in
        # place; the next sweep sends a fresh identifier and timestamp anyway.
        org, _ = self._make_org()
        self._run(org, 150_000, meter_error=StripeError("dup", status=400))
        org.refresh_from_db()
        self.assertIsNone(org.overage_last_reported_at)

    # -- the meter itself ------------------------------------------------------

    def test_refuses_to_report_to_a_sum_meter(self):
        # Cycle-to-date totals into a sum meter compound on every sweep. This is
        # the one unbounded overcharge left, so the send path fails closed.
        org, _ = self._make_org()
        mock_meter = self._run(org, 150_000, formula="sum")
        mock_meter.assert_not_awaited()
        org.refresh_from_db()
        self.assertIsNone(org.overage_last_reported_at)

    def test_meter_is_not_consulted_when_nothing_is_due(self):
        # Under quota or unchanged, the sweep must cost no Stripe call at all;
        # otherwise a Stripe outage turns every metered org into a probe.
        org, _ = self._make_org()
        self._run(org, 150_000)
        find = AsyncMock(return_value=_meter())
        with patch("apps.stripe.client.find_meter", new=find):
            for usage in (50_000, 150_000):
                self.clock += timedelta(seconds=10)
                with (
                    patch(
                        "apps.organizations_ext.tasks.get_event_counts",
                        new=AsyncMock(
                            return_value=EventCounts(issue_event_count=usage)
                        ),
                    ),
                    patch(
                        "apps.organizations_ext.tasks.timezone.now",
                        return_value=self.clock,
                    ),
                    patch(
                        "apps.stripe.client.create_meter_event",
                        new_callable=AsyncMock,
                    ) as mock_meter,
                ):
                    cache.delete("overage-meter-formula:glitchtip_overage_events_total")
                    _check(org.id, bypass_cache=True)
                mock_meter.assert_not_awaited()
        find.assert_not_awaited()

    def test_meter_lookup_failure_is_fail_closed_and_cached(self):
        org, _ = self._make_org()
        find = AsyncMock(side_effect=StripeError("down", status=503))
        with patch("apps.stripe.client.find_meter", new=find):
            for usage in (150_000, 160_000):
                self.clock += timedelta(seconds=10)
                with (
                    patch(
                        "apps.organizations_ext.tasks.get_event_counts",
                        new=AsyncMock(
                            return_value=EventCounts(issue_event_count=usage)
                        ),
                    ),
                    patch(
                        "apps.organizations_ext.tasks.timezone.now",
                        return_value=self.clock,
                    ),
                    patch(
                        "apps.stripe.client.create_meter_event",
                        new_callable=AsyncMock,
                    ) as mock_meter,
                ):
                    _check(org.id, bypass_cache=True)  # must not raise
                mock_meter.assert_not_awaited()
        find.assert_awaited_once()  # the failure is cached, not re-probed per org

    def test_refuses_to_report_without_a_meter(self):
        org, _ = self._make_org()
        with (
            patch("apps.stripe.client.find_meter", new=AsyncMock(return_value=None)),
            patch(
                "apps.stripe.client.create_meter_event", new_callable=AsyncMock
            ) as mock_meter,
            patch(
                "apps.organizations_ext.tasks.get_event_counts",
                new=AsyncMock(return_value=EventCounts(issue_event_count=150_000)),
            ),
        ):
            _check(org.id, bypass_cache=True)
        mock_meter.assert_not_awaited()

    def test_meter_formula_is_cached(self):
        org, _ = self._make_org()
        find = AsyncMock(return_value=_meter())
        with patch("apps.stripe.client.find_meter", new=find):
            for usage in (150_000, 160_000):
                self.clock += timedelta(seconds=10)
                with (
                    patch(
                        "apps.organizations_ext.tasks.get_event_counts",
                        new=AsyncMock(
                            return_value=EventCounts(issue_event_count=usage)
                        ),
                    ),
                    patch(
                        "apps.organizations_ext.tasks.timezone.now",
                        return_value=self.clock,
                    ),
                    patch(
                        "apps.stripe.client.create_meter_event",
                        new_callable=AsyncMock,
                    ),
                ):
                    _check(org.id, bypass_cache=True)
        find.assert_awaited_once()

    # -- eligibility -----------------------------------------------------------

    def test_ineligible_plan_is_not_metered(self):
        """The opt-in flag and the item id are sticky state that outlive the
        plan that justified them; the send path must re-check eligibility."""
        for label, kwargs in (
            ("free", {"plan_price": 0}),
            ("canceled", {"status": SubscriptionStatus.CANCELED}),
            ("unpaid", {"status": SubscriptionStatus.UNPAID}),
        ):
            with self.subTest(label):
                org, _ = self._make_org(stripe_id=f"sub_{label}", **kwargs)
                mock_meter = self._run(org, 150_000)
                mock_meter.assert_not_awaited()
                org.refresh_from_db()
                self.assertIsNone(org.overage_last_reported_at)

    def test_missing_metered_item_is_not_metered(self):
        org, _ = self._make_org(metered_item="")
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()

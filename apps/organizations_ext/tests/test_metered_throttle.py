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

from ..tasks import _cycle_quota, check_organization_throttle

_check = async_to_sync(check_organization_throttle.func)
_cycle_quota_sync = async_to_sync(_cycle_quota)

BASE_PERIOD = object()  # _make_org: the metered item's period is the plan's

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
    billable total, recomputed from current data each time. The local state is
    a record of the last figure sent and the cycle's quota high-water mark.
    These tests pin the properties that make that safe: re-sends restate the
    same figure, a wrong figure is corrected by the next sweep, a lowered cap
    stops growth without retracting billed usage, a smaller quota applies only
    from the next cycle, out-of-order and same-second measurements are skipped,
    a measurement outside the local period is dropped, and nothing is sent to a
    meter that doesn't aggregate by ``last``.

    The window all of this happens in is the metered item's own Stripe period
    once one is attached (``billing_window``), refreshed from Stripe when it
    runs out; the plan's period or virtual month applies only without one.
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
        metered_period=BASE_PERIOD,
        plan_price=15,
        status=SubscriptionStatus.ACTIVE,
        stripe_id="sub_1",
        quota=QUOTA,
    ):
        # Whole seconds: Stripe's timestamps are, and a period that has been
        # through Stripe and back must compare equal to the one it was made from.
        now = timezone.now().replace(microsecond=0)
        org = baker.make(
            "organizations_ext.Organization",
            stripe_customer_id="cus_1",
            metered_billing_enabled=enabled,
            overage_spend_cap_cents=cap_cents,
        )
        product = baker.make("stripe.StripeProduct", events=quota)
        price = baker.make(
            "stripe.StripePrice", product=product, price=plan_price, no_throttle=False
        )
        period = (now - timedelta(days=5), now + timedelta(days=25))
        if metered_period is BASE_PERIOD:
            metered_period = period if metered_item else (None, None)
        sub = StripeSubscription.objects.create(
            stripe_id=stripe_id,
            created=now,
            current_period_start=period[0],
            current_period_end=period[1],
            start_date=now,
            price=price,
            organization=org,
            status=status,
            metered_item_id=metered_item,
            metered_period_start=metered_period[0],
            metered_period_end=metered_period[1],
        )
        org.stripe_primary_subscription = sub
        org.save(update_fields=["stripe_primary_subscription"])
        return org, sub

    def _run(self, org, usage, meter_error=None, at=None, formula="last", fetched=None):
        """Run the throttle check with usage and clock pinned; return the meter mock.

        ``fetched`` is what Stripe answers should the check re-read the
        subscription (a stale metered period); by default Stripe is
        unreachable, so an unexpected refresh leaves the window as it was.
        The counting window the check used is kept in ``self.counted``.
        """
        if at is None:
            self.clock += timedelta(seconds=10)
            at = self.clock
        fetch = (
            AsyncMock(return_value=fetched)
            if fetched is not None
            else AsyncMock(side_effect=StripeError("unreachable", status=503))
        )
        with (
            patch(
                "apps.organizations_ext.tasks.get_event_counts",
                new=AsyncMock(return_value=EventCounts(issue_event_count=usage)),
            ) as counts,
            # ``tasks.timezone`` is django.utils.timezone itself, so this pins
            # the clock for everything the check calls (cycle math included),
            # not just the module. Load-bearing; don't narrow it.
            patch("apps.organizations_ext.tasks.timezone.now", return_value=at),
            patch("apps.stripe.models.fetch_subscription", new=fetch),
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
        self.fetched = fetch
        self.counted = counts.await_args.args[1:] if counts.await_args else None
        return mock_meter

    @staticmethod
    def _stripe_subscription(base_period, metered_period=None):
        """A subscription as Stripe returns it: a base item and maybe a metered one."""

        def item(id, period, recurring):
            it = MagicMock(id=id)
            it.price.recurring = recurring
            it.current_period_start = int(period[0].timestamp())
            it.current_period_end = int(period[1].timestamp())
            return it

        items = [item("si_base", base_period, {"interval": "month"})]
        if metered_period is not None:
            items.append(item("si_1", metered_period, {"usage_type": "metered"}))
        sub = MagicMock(
            status="active",
            created=int(base_period[0].timestamp()),
            start_date=int(base_period[0].timestamp()),
            collection_method="charge_automatically",
        )
        sub.items.data = items
        return sub

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
            metered_period_start=sub_a.metered_period_start + timedelta(seconds=5),
            metered_period_end=sub_a.metered_period_end + timedelta(seconds=5),
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
        # period at zero. In particular the previous cycle's figure is not
        # "corrected" to zero: a zero restatement is only ever a correction
        # within the cycle that holds the figure.
        org, _ = self._make_org()
        org.overage_last_reported_at = timezone.now() - timedelta(days=90)
        org.overage_last_reported_units = 50_000
        org.save(
            update_fields=["overage_last_reported_at", "overage_last_reported_units"]
        )
        mock_meter = self._run(org, 90_000)
        mock_meter.assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)

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

    def test_lowered_cap_floor_still_lets_a_lower_true_total_through(self):
        # The floor is on the cap, not on the figure: a lowered cap can't
        # forgive the month, but it also can't pin a figure the true total no
        # longer supports.
        org, _ = self._make_org()
        self._run(org, 150_000)  # 50,000 reported
        org.overage_spend_cap_cents = 500
        org.save(update_fields=["overage_spend_cap_cents"])
        mock_meter = self._run(org, 130_000)
        self.assertEqual(self._sent(mock_meter), 30_000)

    def test_wrong_high_figure_is_corrected_by_the_next_sweep(self):
        """Every report is recomputed from current data, so whatever put a
        too-high figure on the meter, the next sweep restates the right one
        and ``last`` takes it. This is what the old output floor prevented."""
        org, _ = self._make_org()
        org.overage_last_reported_units = 400_000
        org.overage_last_reported_at = self.clock - timedelta(minutes=5)
        org.save(
            update_fields=["overage_last_reported_units", "overage_last_reported_at"]
        )
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 50_000)

    # -- the cycle's quota -----------------------------------------------------

    def test_downgrade_mid_cycle_keeps_the_larger_quota(self):
        # Usage that was within quota when it happened must not become billable
        # because the plan shrank afterwards. The smaller quota applies from
        # the next cycle.
        org, sub = self._make_org(quota=1_000_000)
        self._run(org, 500_000).assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

        sub.price.product.events = 100_000
        sub.price.product.save(update_fields=["events"])
        self._run(org, 500_000).assert_not_awaited()
        mock_meter = self._run(org, 1_050_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

    def test_upgrade_mid_cycle_forgives_overage_for_the_cycle(self):
        # The larger quota covers the whole cycle, so overage already billed
        # under the smaller plan is restated downwards -- to zero here -- and
        # the meter's ``last`` takes it.
        org, sub = self._make_org()
        self.assertEqual(self._sent(self._run(org, 150_000)), 50_000)

        sub.price.product.events = 1_000_000
        sub.price.product.save(update_fields=["events"])
        mock_meter = self._run(org, 160_000)
        self.assertEqual(self._sent(mock_meter), 0)
        org.refresh_from_db()
        self.assertEqual(org.overage_last_reported_units, 0)
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

    def test_cycle_quota_resets_on_rollover(self):
        org, _ = self._make_org()
        org.overage_cycle_start = timezone.now() - timedelta(days=40)
        org.overage_cycle_quota = 1_000_000
        org.save(update_fields=["overage_cycle_start", "overage_cycle_quota"])
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, QUOTA)

    def test_cycle_quota_tolerates_a_stale_copy_of_the_record(self):
        # The sweep works from a batch copy that can be minutes old. If an
        # ingest re-check has already stamped this cycle (and seen a larger
        # plan), the sweep must neither refuse to report nor lower the mark.
        org, sub = self._make_org()
        start = sub.current_period_start
        Organization.objects.filter(id=org.id).update(
            overage_cycle_start=start, overage_cycle_quota=1_000_000
        )
        self.assertIsNone(org.overage_cycle_start)  # the stale copy
        self.assertEqual(_cycle_quota_sync(org, start, QUOTA), 1_000_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

    def test_cycle_quota_follows_an_anchor_that_moved_backwards(self):
        # A primary subscription swapped for one on an earlier anchor (a lapsed
        # monthly plan giving way to an annual one, say) is a new cycle, not a
        # stale check: the caller only asks about the cycle its measurement is
        # in. Refusing it would silence reporting until the old anchor passed.
        org, sub = self._make_org()
        start = sub.current_period_start
        Organization.objects.filter(id=org.id).update(
            overage_cycle_start=start, overage_cycle_quota=QUOTA
        )
        earlier = start - timedelta(days=11)
        self.assertEqual(_cycle_quota_sync(org, earlier, 1_000_000), 1_000_000)
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_start, earlier)

    def test_cycle_quota_is_tracked_while_overage_is_off(self):
        # The downgrade the mark protects against can come before the opt-in.
        org, sub = self._make_org(enabled=False, quota=1_000_000)
        self._run(org, 900_000).assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

        sub.price.product.events = 100_000
        sub.price.product.save(update_fields=["events"])
        org.metered_billing_enabled = True
        org.save(update_fields=["metered_billing_enabled"])
        # 900,000 used against a 100,000 plan would be the whole cap; it was
        # all within quota when it happened.
        self._run(org, 900_000).assert_not_awaited()
        mock_meter = self._run(org, 1_010_000)
        self.assertEqual(self._sent(mock_meter), 10_000)

    def test_stale_check_does_not_restamp_the_cycle_record(self):
        org, sub = self._make_org()
        Organization.objects.filter(id=org.id).update(
            overage_cycle_start=timezone.now() + timedelta(days=1),
            overage_cycle_quota=1_000_000,
        )
        StripeSubscription.objects.filter(pk=sub.pk).update(
            metered_period_start=timezone.now() - timedelta(days=40),
            metered_period_end=timezone.now() - timedelta(minutes=1),
        )
        org.refresh_from_db()
        self._run(org, 150_000).assert_not_awaited()
        org.refresh_from_db()
        self.assertEqual(org.overage_cycle_quota, 1_000_000)

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
        billed twice. A measurement past the local period end is dropped (and
        with Stripe unreachable, as here, the period can't be refreshed).
        """
        org, sub = self._make_org()
        self._run(org, 150_000)  # 50,000 reported in the current cycle
        StripeSubscription.objects.filter(pk=sub.pk).update(
            metered_period_start=timezone.now() - timedelta(days=40),
            metered_period_end=timezone.now() - timedelta(minutes=1),
        )
        org.refresh_from_db()
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()
        self.fetched.assert_awaited()  # it did ask Stripe
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
            metered_period_start=timezone.now() + timedelta(days=1),
            metered_period_end=timezone.now() + timedelta(days=31),
        )
        mock_meter = self._run(org, 150_000)
        mock_meter.assert_not_awaited()
        org.refresh_from_db()
        self.assertIsNone(org.overage_last_reported_at)

    # -- the window is the metered item's period --------------------------------

    def test_metered_item_period_is_the_window_not_the_plans(self):
        """Stripe files the event under the metered item's period, so that is
        what usage is counted over and reported against, even where the plan's
        own period says otherwise."""
        now = self.clock
        item_period = (now - timedelta(days=2), now + timedelta(days=28))
        org, _ = self._make_org(metered_period=item_period)
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        self.assertEqual(self.counted, item_period)
        org.refresh_from_db()
        # The cycle's quota record is keyed on the same window.
        self.assertEqual(org.overage_cycle_start, item_period[0])

    def test_annual_plan_meters_in_the_items_period_not_the_virtual_month(self):
        """The virtual month is local arithmetic over the annual period, and
        Stripe knows nothing of it. With a metered item attached the item's
        period is the window, however stale or fresh the virtual month is."""
        org, sub = self._make_org()
        now = timezone.now()
        StripeSubscription.objects.filter(pk=sub.pk).update(
            current_period_start=now - timedelta(days=200),
            current_period_end=now + timedelta(days=165),
            subscription_cycle_start=now - timedelta(days=35),
            subscription_cycle_end=now - timedelta(days=5),
        )
        org.refresh_from_db()
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._sent(mock_meter), 50_000)
        self.assertEqual(
            self.counted, (sub.metered_period_start, sub.metered_period_end)
        )

    def test_window_stays_put_while_overage_is_off(self):
        # The item stays attached when overage is switched off, and so does
        # its window: toggling can't move the count to a fresh period.
        now = self.clock
        item_period = (now - timedelta(days=2), now + timedelta(days=28))
        org, _ = self._make_org(enabled=False, metered_period=item_period)
        self._run(org, 150_000).assert_not_awaited()
        self.assertEqual(self.counted, item_period)
        self.assertEqual(self._throttle(org), 10)

    def test_without_an_item_the_plans_period_is_the_window(self):
        org, sub = self._make_org(enabled=False, metered_item="")
        self._run(org, 50_000)
        self.assertEqual(
            self.counted, (sub.current_period_start, sub.current_period_end)
        )
        self.fetched.assert_not_awaited()

    def test_run_out_metered_period_is_refreshed_from_stripe(self):
        """When the item's period has ended and no webhook has said what the
        next one is, the check asks Stripe rather than guessing, then counts
        and reports in the period Stripe answered with."""
        now = self.clock
        org, sub = self._make_org(
            metered_period=(now - timedelta(days=32), now - timedelta(hours=1))
        )
        new_period = (now - timedelta(hours=1), now + timedelta(days=29))
        fetched = self._stripe_subscription(
            (sub.current_period_start, sub.current_period_end), new_period
        )
        mock_meter = self._run(org, 150_000, fetched=fetched)
        self.assertEqual(self._sent(mock_meter), 50_000)
        self.assertEqual(self.counted, new_period)
        sub.refresh_from_db()
        self.assertEqual(sub.metered_period_start, new_period[0])
        self.assertEqual(sub.metered_period_end, new_period[1])

    def test_unrecorded_metered_period_is_fetched(self):
        # An item attached before its period was recorded (or by a webhook
        # that hasn't landed) is looked up rather than left on the plan's window.
        now = self.clock
        org, sub = self._make_org(metered_period=(None, None))
        item_period = (now - timedelta(days=3), now + timedelta(days=27))
        fetched = self._stripe_subscription(
            (sub.current_period_start, sub.current_period_end), item_period
        )
        self._run(org, 150_000, fetched=fetched)
        self.assertEqual(self.counted, item_period)

    def test_refresh_is_rate_limited_per_subscription(self):
        # A Stripe outage during a sweep costs one call per subscription every
        # few minutes, not one per check.
        now = self.clock
        org, _ = self._make_org(
            metered_period=(now - timedelta(days=32), now - timedelta(hours=1))
        )
        self._run(org, 150_000).assert_not_awaited()
        self.fetched.assert_awaited_once()
        self._run(org, 150_000).assert_not_awaited()
        self.fetched.assert_not_awaited()

    def test_failed_refresh_keeps_the_stale_window_and_reports_nothing(self):
        now = self.clock
        stale = (now - timedelta(days=32), now - timedelta(hours=1))
        org, sub = self._make_org(metered_period=stale)
        self._run(org, 300_000).assert_not_awaited()
        self.assertEqual(self.counted, stale)
        sub.refresh_from_db()
        self.assertEqual(sub.metered_period_end, stale[1])
        # The throttle is still set from what was counted: well past quota
        # and cap (300,000 against 233,333 allowed).
        self.assertEqual(self._throttle(org), 50)

    def test_unknown_metered_period_throttles_but_does_not_report(self):
        """An item whose period Stripe hasn't told us yet, with Stripe down.
        Counting over the plan's window is fine for the throttle; reporting
        that total would file it under the item's period, and where the two
        differ the overlap is billed twice. So: throttled, not reported."""
        org, sub = self._make_org(metered_period=(None, None))
        self._run(org, 150_000).assert_not_awaited()
        self.fetched.assert_awaited_once()
        self.assertEqual(
            self.counted, (sub.current_period_start, sub.current_period_end)
        )
        # Over quota with no paid headroom to count on: the plain ramp.
        self.assertEqual(self._throttle(org), 10)
        org.refresh_from_db()
        self.assertIsNone(org.overage_last_reported_at)

    def test_refresh_that_finds_the_subscription_canceled_drops_the_item(self):
        # As the webhook does on a terminal status: the item died with the
        # subscription, and a kept id would let a later enable reuse it.
        now = self.clock
        org, sub = self._make_org(
            metered_period=(now - timedelta(days=32), now - timedelta(hours=1))
        )
        fetched = self._stripe_subscription(
            (sub.current_period_start, sub.current_period_end),
            (now - timedelta(hours=1), now + timedelta(days=29)),
        )
        fetched.status = "canceled"
        self._run(org, 150_000, fetched=fetched).assert_not_awaited()
        sub.refresh_from_db()
        self.assertEqual(sub.status, "canceled")
        self.assertEqual(sub.metered_item_id, "")
        self.assertIsNone(sub.metered_period_start)

    def test_item_gone_at_stripe_falls_back_to_the_plans_period(self):
        # The item was removed in the Stripe dashboard: the refresh clears it
        # and the plan's period is the window again. Nothing is reported.
        now = self.clock
        org, sub = self._make_org(
            metered_period=(now - timedelta(days=32), now - timedelta(hours=1))
        )
        fetched = self._stripe_subscription(
            (sub.current_period_start, sub.current_period_end), metered_period=None
        )
        self._run(org, 150_000, fetched=fetched).assert_not_awaited()
        self.assertEqual(
            self.counted, (sub.current_period_start, sub.current_period_end)
        )
        sub.refresh_from_db()
        self.assertEqual(sub.metered_item_id, "")
        self.assertIsNone(sub.metered_period_end)

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
        # otherwise a Stripe outage turns every metered org into a probe. (A
        # total that *drops* within a cycle is a correction and does go out.)
        org, _ = self._make_org()
        self._run(org, 150_000)
        under_quota, _ = self._make_org(stripe_id="sub_2")
        find = AsyncMock(return_value=_meter())
        with patch("apps.stripe.client.find_meter", new=find):
            for target, usage in ((under_quota, 50_000), (org, 150_000)):
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
                    _check(target.id, bypass_cache=True)
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

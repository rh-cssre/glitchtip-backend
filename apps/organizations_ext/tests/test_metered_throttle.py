from datetime import timedelta
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from django.utils import timezone
from model_bakery import baker

from apps.organizations_ext.models import EventCounts, Organization
from apps.stripe.constants import SubscriptionStatus
from apps.stripe.exceptions import StripeError
from apps.stripe.models import StripeSubscription

from ..tasks import _persist_overage, _rollback_overage, check_organization_throttle

_check = async_to_sync(check_organization_throttle.func)

QUOTA = 100_000
# $20 cap at $0.00015/event (first tier) -> 133,333 affordable overage units.
CAP_CENTS = 2000
CAP_UNITS = 133_333
ALLOWED = QUOTA + CAP_UNITS  # 233,333


@override_settings(
    BILLING_ENABLED=True,
    GLITCHTIP_OVERAGE_TIERS=[
        (400_000, "0.00015"),
        (2_000_000, "0.00010"),
        (None, "0.00008"),
    ],
)
class MeteredThrottleTestCase(TestCase):
    def _make_org(self, *, enabled=True, cap_cents=CAP_CENTS, metered_item="si_1"):
        now = timezone.now()
        org = baker.make(
            "organizations_ext.Organization",
            stripe_customer_id="cus_1",
            metered_billing_enabled=enabled,
            overage_spend_cap_cents=cap_cents,
        )
        product = baker.make("stripe.StripeProduct", events=QUOTA)
        price = baker.make(
            "stripe.StripePrice", product=product, price=15, no_throttle=False
        )
        sub = StripeSubscription.objects.create(
            stripe_id="sub_1",
            created=now,
            current_period_start=now - timedelta(days=5),
            current_period_end=now + timedelta(days=25),
            start_date=now,
            price=price,
            organization=org,
            status=SubscriptionStatus.ACTIVE,
            metered_item_id=metered_item,
        )
        org.stripe_primary_subscription = sub
        org.save(update_fields=["stripe_primary_subscription"])
        return org, sub

    def _run(self, org, usage, meter_error=None):
        """Run the throttle check with usage pinned, returning the meter mock."""
        with (
            patch(
                "apps.organizations_ext.tasks.get_event_counts",
                new=AsyncMock(return_value=EventCounts(issue_event_count=usage)),
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

    def test_under_quota_no_throttle_no_report(self):
        org, _ = self._make_org()
        mock_meter = self._run(org, 50_000)
        self.assertEqual(self._throttle(org), 0)
        mock_meter.assert_not_awaited()

    def test_within_cap_reports_overage_no_throttle(self):
        org, sub = self._make_org()
        mock_meter = self._run(org, 150_000)
        self.assertEqual(self._throttle(org), 0)
        # Reports the overage delta (50,000) as the meter value.
        mock_meter.assert_awaited_once()
        self.assertEqual(mock_meter.await_args.args[2], 50_000)
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 50_000)

    def test_just_past_cap_ramps_to_ten(self):
        org, _ = self._make_org()
        mock_meter = self._run(org, ALLOWED + 1)
        self.assertEqual(self._throttle(org), 10)
        # Billable is capped at the budget; never report beyond CAP_UNITS.
        self.assertEqual(mock_meter.await_args.args[2], CAP_UNITS)

    def test_ramp_fifty(self):
        org, _ = self._make_org()
        self._run(org, ALLOWED + QUOTA // 2 + 1)
        self.assertEqual(self._throttle(org), 50)

    def test_ramp_hundred_block(self):
        org, _ = self._make_org()
        self._run(org, ALLOWED + QUOTA + 1)
        self.assertEqual(self._throttle(org), 100)

    def test_report_is_idempotent_across_checks(self):
        org, sub = self._make_org()
        self._run(org, 150_000)
        mock_meter = self._run(org, 150_000)
        # Second check sees no increase, so it reports nothing new.
        mock_meter.assert_not_awaited()

    def test_cycle_rollover_resets_counter(self):
        org, sub = self._make_org()
        # Pretend a prior cycle already reported a large amount.
        sub.overage_period_start = timezone.now() - timedelta(days=90)
        sub.overage_units_reported = 999_999
        sub.save(update_fields=["overage_period_start", "overage_units_reported"])

        mock_meter = self._run(org, 150_000)
        sub.refresh_from_db()
        # Counter reset to the new cycle, delta reported from zero.
        self.assertEqual(sub.overage_units_reported, 50_000)
        mock_meter.assert_awaited_once()
        self.assertEqual(mock_meter.await_args.args[2], 50_000)

    def test_disabled_uses_base_ramp_no_report(self):
        # Metering off: usage past quota throttles on the base ramp, no charges.
        org, _ = self._make_org(enabled=False)
        mock_meter = self._run(org, 160_000)  # > 1.5x quota -> 50%
        self.assertEqual(self._throttle(org), 50)
        mock_meter.assert_not_awaited()

    def test_enabled_without_item_uses_base_ramp(self):
        # Enabled but no Stripe item attached yet -> falls back to base ramp.
        org, _ = self._make_org(metered_item="")
        mock_meter = self._run(org, 250_000)  # > 2x quota -> 100%
        self.assertEqual(self._throttle(org), 100)
        mock_meter.assert_not_awaited()

    def test_toggle_off_then_on_does_not_rereport(self):
        # Mid-cycle double-charge regression: after reporting overage, a
        # disable + re-enable within the same cycle must report only the new
        # delta, never the units already metered.
        org, sub = self._make_org()
        self._run(org, 150_000)  # reports 50,000; counter -> 50,000

        # Disable mirrors the API: flag off, counter and item left intact.
        org.metered_billing_enabled = False
        org.save(update_fields=["metered_billing_enabled"])
        self._run(org, 170_000)  # disabled -> no report
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 50_000)

        # Re-enable in the same cycle and grow usage; only the increment reports.
        org.metered_billing_enabled = True
        org.save(update_fields=["metered_billing_enabled"])
        mock_meter = self._run(org, 180_000)  # overage 80,000; delta 30,000
        mock_meter.assert_awaited_once()
        self.assertEqual(mock_meter.await_args.args[2], 30_000)
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 80_000)

    def test_identifier_keyed_on_base_counter(self):
        # The identifier must encode the counter the delta was computed FROM,
        # so a re-send that raced a lost persist dedupes at Stripe instead of
        # double-billing (the delta itself may differ between the two sends).
        org, sub = self._make_org()
        mock_meter = self._run(org, 150_000)
        sub.refresh_from_db()
        cycle = sub.overage_period_start.isoformat()
        self.assertEqual(mock_meter.await_args.args[3], f"sub_1:{cycle}:0")

        mock_meter = self._run(org, 180_000)  # delta 30,000 from base 50,000
        self.assertEqual(mock_meter.await_args.args[3], f"sub_1:{cycle}:50000")

    def test_transient_stripe_error_retries_same_report(self):
        # A 5xx must not advance the counter: the next check re-sends the same
        # delta under the same identifier, so the retry can't double-bill.
        org, sub = self._make_org()
        mock_meter = self._run(org, 150_000, meter_error=StripeError("", status=503))
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 0)

        retry_meter = self._run(org, 150_000)
        retry_meter.assert_awaited_once()
        self.assertEqual(retry_meter.await_args.args[2], 50_000)
        self.assertEqual(retry_meter.await_args.args[3], mock_meter.await_args.args[3])

    def test_bad_request_advances_counter(self):
        # A 400 (e.g. duplicate identifier) can never succeed on retry; the
        # counter advances so the check doesn't loop on it — and doesn't
        # eventually outlive Stripe's uniqueness window and double-bill.
        org, sub = self._make_org()
        self._run(org, 150_000, meter_error=StripeError("dup", status=400))
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 50_000)

        mock_meter = self._run(org, 150_000)  # no growth -> nothing to report
        mock_meter.assert_not_awaited()

    def test_cap_lowered_then_restored_bills_only_growth(self):
        # Lowering the cap below already-reported units must not regress the
        # counter — a lower base would re-bill recorded units on later growth.
        org, sub = self._make_org()
        self._run(org, 150_000)  # counter -> 50,000

        org.overage_spend_cap_cents = 500  # ~3,333 affordable units
        org.save(update_fields=["overage_spend_cap_cents"])
        mock_meter = self._run(org, 160_000)
        mock_meter.assert_not_awaited()
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 50_000)

        org.overage_spend_cap_cents = CAP_CENTS
        org.save(update_fields=["overage_spend_cap_cents"])
        mock_meter = self._run(org, 160_000)  # overage 60,000; delta 10,000
        self.assertEqual(mock_meter.await_args.args[2], 10_000)

    def test_stale_persist_cannot_regress_counter(self):
        # A writer that lost the lock race must not move the counter backwards:
        # deltas recomputed from the lower base would re-bill recorded units.
        org, sub = self._make_org()
        self._run(org, 180_000)  # counter -> 80,000
        sub.refresh_from_db()
        async_to_sync(_persist_overage)(sub, sub.overage_period_start, 50_000)
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 80_000)

    def test_rollback_only_reverts_own_write(self):
        # Rollback is conditional on the counter still holding the write-ahead
        # value; a superseded write must not be stomped back.
        org, sub = self._make_org()
        self._run(org, 150_000)  # counter -> 50,000
        sub.refresh_from_db()
        async_to_sync(_rollback_overage)(sub, sub.overage_period_start, 40_000, 10_000)
        sub.refresh_from_db()
        self.assertEqual(sub.overage_units_reported, 50_000)

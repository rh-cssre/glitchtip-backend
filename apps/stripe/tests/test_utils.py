from datetime import datetime
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from django.test import SimpleTestCase
from django.utils.timezone import make_aware

from apps.stripe.utils import _MAX_CYCLE_MONTHS, compute_cycle


def dt(year, month, day):
    return make_aware(datetime(year, month, day))


class ComputeCycleTests(SimpleTestCase):
    def test_monthly_returns_full_period(self):
        start = dt(2026, 1, 1)
        end = dt(2026, 2, 1)
        self.assertEqual(compute_cycle(start, end, is_annual=False), (start, end))

    def test_annual_first_month(self):
        """Period just started — cycle should be the first month."""
        start = dt(2026, 1, 15)
        end = dt(2027, 1, 15)
        now = dt(2026, 1, 20)
        with patch("apps.stripe.utils.timezone.now", return_value=now):
            cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
        self.assertEqual(cycle_start, dt(2026, 1, 15))
        self.assertEqual(cycle_end, dt(2026, 2, 15))

    def test_annual_month_end_drift(self):
        """Jan 31 start checked on Mar 15: cycle must be Feb 28 → Mar 31.

        The bug: chaining relativedelta from cycle_end loses the day-of-month
        anchor after February.  Jan 31 + 1m = Feb 28, then Feb 28 + 1m = Mar 28
        instead of the correct Mar 31.
        """
        start = dt(2026, 1, 31)
        end = dt(2027, 1, 31)
        now = dt(2026, 3, 15)
        with patch("apps.stripe.utils.timezone.now", return_value=now):
            cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
        # period_start + 2 months = Mar 31 (relativedelta anchors to day 31)
        self.assertEqual(cycle_start, dt(2026, 2, 28))
        self.assertEqual(cycle_end, dt(2026, 3, 31))

    def test_annual_month_end_drift_further(self):
        """Jan 31 start checked on May 15: cycle must be Apr 30 → May 31."""
        start = dt(2026, 1, 31)
        end = dt(2027, 1, 31)
        now = dt(2026, 5, 15)
        with patch("apps.stripe.utils.timezone.now", return_value=now):
            cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
        self.assertEqual(cycle_start, dt(2026, 4, 30))
        self.assertEqual(cycle_end, dt(2026, 5, 31))

    def test_annual_five_months_in(self):
        """Mid-year check should not be stuck on month 1."""
        start = dt(2025, 10, 1)
        end = dt(2026, 10, 1)
        now = dt(2026, 3, 15)
        with patch("apps.stripe.utils.timezone.now", return_value=now):
            cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
        self.assertEqual(cycle_start, dt(2026, 3, 1))
        self.assertEqual(cycle_end, dt(2026, 4, 1))
        self.assertLessEqual(cycle_start, now)
        self.assertGreaterEqual(cycle_end, now)

    def test_annual_boundaries_anchored_to_period_start(self):
        """Cycle boundaries must always be period_start + N months."""
        start = dt(2026, 1, 31)
        end = dt(2027, 1, 31)
        for month_offset in range(1, 12):
            now = start + relativedelta(months=month_offset, days=1)
            with patch("apps.stripe.utils.timezone.now", return_value=now):
                cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
            # cycle_end must equal period_start + (N+1) months for some N
            self.assertEqual(
                cycle_end,
                start + relativedelta(months=month_offset + 1),
                f"cycle_end not anchored at month offset {month_offset}",
            )
            # cycle_start must equal period_start + N months
            self.assertEqual(
                cycle_start,
                start + relativedelta(months=month_offset),
                f"cycle_start not anchored at month offset {month_offset}",
            )
            # Cycle must cover now
            self.assertLessEqual(cycle_start, now)
            self.assertGreaterEqual(cycle_end, now)

    def test_annual_stale_period_start_caps_at_max(self):
        """A period_start far in the past should not loop forever."""
        start = dt(2010, 6, 15)
        end = dt(2011, 6, 15)
        now = dt(2026, 3, 15)
        with (
            patch("apps.stripe.utils.timezone.now", return_value=now),
            patch("apps.stripe.utils.logger") as mock_logger,
        ):
            cycle_start, cycle_end = compute_cycle(start, end, is_annual=True)
        # Should have stopped at the cap, not looped to 2026
        self.assertEqual(
            cycle_start,
            start + relativedelta(months=_MAX_CYCLE_MONTHS),
        )
        mock_logger.warning.assert_called_once()

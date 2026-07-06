import os
import time
import unittest
from datetime import datetime
from datetime import timezone as dt_timezone
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from django.test import SimpleTestCase
from django.utils.timezone import make_aware

from apps.stripe.utils import (
    _MAX_CYCLE_MONTHS,
    compute_cycle,
    compute_cycle_n_ago,
    unix_to_datetime,
)


def dt(year, month, day):
    return make_aware(datetime(year, month, day))


@unittest.skipUnless(
    hasattr(time, "tzset"), "time.tzset() not available on this platform"
)
class UnixToDatetimeTests(SimpleTestCase):
    """`unix_to_datetime` must return a correct UTC moment regardless of
    the process's local timezone, since deployments may set ``TZ`` to
    something other than UTC for log readability."""

    def setUp(self):
        self._saved_tz = os.environ.get("TZ")
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved_tz
        time.tzset()

    def _expected(self, timestamp: int) -> datetime:
        return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)

    def test_returns_correct_utc_under_utc_local(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        ts = 1778333083  # 2026-05-09 13:24:43 UTC
        self.assertEqual(unix_to_datetime(ts), self._expected(ts))

    def test_returns_correct_utc_under_eastern_local(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        ts = 1778333083
        self.assertEqual(unix_to_datetime(ts), self._expected(ts))

    def test_returns_correct_utc_under_tokyo_local(self):
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        ts = 1778333083
        self.assertEqual(unix_to_datetime(ts), self._expected(ts))


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


class ComputeCycleNAgoTests(SimpleTestCase):
    def test_monthly_plan(self):
        result = compute_cycle_n_ago(
            current_period_start=dt(2026, 3, 15),
            current_period_end=dt(2026, 4, 15),
            subscription_cycle_start=None,
            subscription_cycle_end=None,
            periods_ago=1,
        )
        self.assertEqual(result, (dt(2026, 2, 15), dt(2026, 3, 15)))

    def test_annual_plan_with_previous_cycle(self):
        result = compute_cycle_n_ago(
            current_period_start=dt(2025, 10, 1),
            current_period_end=dt(2026, 10, 1),
            subscription_cycle_start=dt(2026, 3, 1),
            subscription_cycle_end=dt(2026, 4, 1),
            periods_ago=1,
        )
        self.assertEqual(result, (dt(2026, 2, 1), dt(2026, 3, 1)))

    def test_annual_plan_first_month_returns_none(self):
        result = compute_cycle_n_ago(
            current_period_start=dt(2026, 1, 15),
            current_period_end=dt(2027, 1, 15),
            subscription_cycle_start=dt(2026, 1, 15),
            subscription_cycle_end=dt(2026, 2, 15),
            periods_ago=1,
        )
        self.assertIsNone(result)

    def test_monthly_plan_jan_to_feb(self):
        result = compute_cycle_n_ago(
            current_period_start=dt(2026, 1, 1),
            current_period_end=dt(2026, 2, 1),
            subscription_cycle_start=None,
            subscription_cycle_end=None,
            periods_ago=1,
        )
        self.assertEqual(result, (dt(2025, 12, 1), dt(2026, 1, 1)))

    def test_monthly_plan_two_periods_ago(self):
        result = compute_cycle_n_ago(
            current_period_start=dt(2026, 3, 15),
            current_period_end=dt(2026, 4, 15),
            subscription_cycle_start=None,
            subscription_cycle_end=None,
            periods_ago=2,
        )
        self.assertEqual(result, (dt(2026, 1, 15), dt(2026, 2, 15)))

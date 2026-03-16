import logging
from datetime import datetime

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.utils import timezone
from django.utils.timezone import make_aware

logger = logging.getLogger(__name__)

# Safety cap for the monthly-cycle advancement loop.  120 months (10 years)
# is far beyond any realistic catch-up window but prevents a runaway loop
# if period_start is corrupt or absurdly old.
_MAX_CYCLE_MONTHS = 120


def get_stripe_link(stripe_id: str) -> str:
    base = "https://dashboard.stripe.com"
    path = "/"
    key = settings.STRIPE_SECRET_KEY
    if key and key.startswith("sk_test"):
        path += "test/"
    if stripe_id.startswith("sub"):
        path += "subscriptions/"
    if stripe_id.startswith("prod"):
        path += "products/"
    if stripe_id.startswith("cus"):
        path += "customers/"
    return f"{base}{path}{stripe_id}"


def unix_to_datetime(timestamp: int) -> datetime:
    return make_aware(datetime.fromtimestamp(timestamp))


def compute_cycle(period_start: datetime, period_end: datetime, is_annual: bool):
    """Return (cycle_start, cycle_end) for a subscription period.

    Monthly plans use the full Stripe billing period.  Annual plans are
    subdivided into virtual monthly cycles for quota counting — this
    function advances from period_start until the cycle covers now.
    """
    if not is_annual:
        return period_start, period_end

    now = timezone.now()
    month = 0
    cycle_start = period_start
    cycle_end = period_start + relativedelta(months=1)
    while cycle_end < now and month < _MAX_CYCLE_MONTHS:
        month += 1
        cycle_start = period_start + relativedelta(months=month)
        cycle_end = period_start + relativedelta(months=month + 1)
    if month >= _MAX_CYCLE_MONTHS:
        logger.warning(
            "compute_cycle hit %d-month cap; period_start=%s is likely stale",
            _MAX_CYCLE_MONTHS,
            period_start,
        )
    return cycle_start, cycle_end

from datetime import datetime

from django.conf import settings
from django.utils.timezone import make_aware


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

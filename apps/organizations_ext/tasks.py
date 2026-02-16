import logging

from asgiref.sync import sync_to_async
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.cache import cache
from django.tasks import task
from django.utils import timezone

from apps.stripe.constants import SubscriptionStatus
from apps.stripe.models import StripeSubscription

from .email import InvitationEmail, ThrottleNoticeEmail
from .models import Organization

logger = logging.getLogger(__name__)


def get_free_tier_cycle(created):
    """
    Calculate the current billing cycle for a free tier organization.
    Anchored to the organization's creation date.
    """
    now = timezone.now()
    if created > now:
        return created, created + relativedelta(months=1)

    # Calculate the number of months between created and now
    # We want the start date to be in the past (or now) and end date in the future
    # created + N months <= now < created + N+1 months

    # Simple approach: set year/month to now, keep day
    # Handle edge cases like Jan 31 -> Feb 28

    # Calculate months difference
    months_diff = (now.year - created.year) * 12 + now.month - created.month

    candidate_start = created + relativedelta(months=months_diff)

    if candidate_start > now:
        months_diff -= 1
        candidate_start = created + relativedelta(months=months_diff)

    return candidate_start, candidate_start + relativedelta(months=1)


@task
async def update_subscription_cycles():
    """
    Daily task to update subscription cycle dates for annual plans.
    """
    now = timezone.now()
    # Find active subscriptions where the cycle has ended
    # We only care about annual plans (interval='year') usually, but strictly speaking
    # checking subscription_cycle_end < now is enough if we assume monthly plans are updated by Stripe sync.
    # However, to be safe and efficient, we can target those that need rolling.

    # We need to filter for subscriptions that are active and have a cycle end in the past
    qs = StripeSubscription.objects.filter(
        status=SubscriptionStatus.ACTIVE,
        subscription_cycle_end__lt=now,
        price__interval="year",
    ).select_related("price")

    async for sub in qs.aiterator():
        # Advance cycle by one month until it covers now
        while sub.subscription_cycle_end < now:
            sub.subscription_cycle_start = sub.subscription_cycle_end
            sub.subscription_cycle_end = sub.subscription_cycle_start + relativedelta(
                months=1
            )

        await sub.asave(
            update_fields=["subscription_cycle_start", "subscription_cycle_end"]
        )


@task
async def check_organization_throttle(organization_id: int, bypass_cache: bool = False):
    if not bypass_cache and not await cache.aadd(
        f"org-throttle-{organization_id}", True
    ):
        return  # Recent check already performed

    org = await (
        Organization.objects.with_event_counts()
        .select_related("stripe_primary_subscription__price__product")
        .aget(id=organization_id)
    )
    await _check_and_update_throttle(org)


@task
async def check_all_organizations_throttle():
    async for org in (
        Organization.objects.with_event_counts()
        .select_related("stripe_primary_subscription__price__product")
        .aiterator()
    ):
        await _check_and_update_throttle(org)


async def _check_and_update_throttle(org: Organization):
    if not settings.BILLING_ENABLED:
        return

    plan_events: int | None = None

    if org.stripe_primary_subscription:
        price = org.stripe_primary_subscription.price
        if price.no_throttle:
            org_throttle = 0
            # Early return? No, we need to update DB if it changed
        else:
            plan_events = price.product.events
            org_throttle = 0

            # Count is already accurate from with_event_counts (using subscription_cycle fields)
            if plan_events is None or org.total_event_count > plan_events * 2:
                org_throttle = 100
            elif org.total_event_count > plan_events * 1.5:
                org_throttle = 50
            elif org.total_event_count > plan_events:
                org_throttle = 10

        # Logic for sending email / saving is at the end
    else:
        # Free Tier
        plan_events = settings.GLITCHTIP_FREE_TIER_EVENTS

        # For free tier, we must ensure we are using the Anchored Cycle count
        # The default with_event_counts uses Rolling 30 Days (or whatever fallback)
        # So we should re-query with the precise Anchored Date
        start, end = get_free_tier_cycle(org.created)

        # We need to fetch the count for this specific range
        # We can reuse with_event_counts but filtering for this specific org and range
        # This is an extra query, but necessary for accuracy of Anchored Free Tier
        free_org = await Organization.objects.with_event_counts(
            start=start, end=end
        ).aget(id=org.id)
        current_count = free_org.total_event_count

        org_throttle = 0
        if current_count > plan_events * 2:
            org_throttle = 100
        elif current_count > plan_events * 1.5:
            org_throttle = 50
        elif current_count > plan_events:
            org_throttle = 10

    if org.event_throttle_rate != org_throttle:
        old_throttle = org.event_throttle_rate
        org.event_throttle_rate = org_throttle
        await org.asave(update_fields=["event_throttle_rate"])
        if org_throttle > old_throttle:
            await send_throttle_email.aenqueue(org.id)


@task
async def send_throttle_email(organization_id: int):
    await sync_to_async(ThrottleNoticeEmail(pk=organization_id).send_email)()


@task
async def send_email_invite(org_user_id: int, token: str):
    await sync_to_async(InvitationEmail(pk=org_user_id, token=token).send_email)()


@task
async def delete_organization(organization_id: int):
    """Delete cold storage files for an org, then hard-delete from DB."""
    org = await Organization.objects.aget(id=organization_id)

    # Delete cold storage files before removing DB rows
    await sync_to_async(_delete_org_cold_storage)(org.id)

    await sync_to_async(org.force_delete)()
    logger.info("Organization %s (id=%s) fully deleted", org.slug, org.id)


def _delete_org_cold_storage(org_id: int):
    from glitchtip.cold_storage import (
        delete_org_cold_storage,
        is_duckdb_available,
    )

    if not is_duckdb_available():
        return

    table_names = ["logs_logevent", "issue_events_issueevent"]
    for table_name in table_names:
        delete_org_cold_storage(org_id, table_name)

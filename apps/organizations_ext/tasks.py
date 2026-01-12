from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.tasks import task

from .email import InvitationEmail, ThrottleNoticeEmail
from .models import Organization


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
    plan_events: int | None = None
    if org.stripe_primary_subscription:
        plan_events = org.stripe_primary_subscription.price.product.events
    org_throttle = 0
    if plan_events is None or org.total_event_count > plan_events * 2:
        org_throttle = 100
    elif org.total_event_count > plan_events * 1.5:
        org_throttle = 50
    elif org.total_event_count > plan_events:
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

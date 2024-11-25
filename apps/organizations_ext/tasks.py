from celery import shared_task
from django.core.cache import cache

from .email import InvitationEmail, MetQuotaEmail
from .models import Organization


@shared_task
def check_organization_throttle(organization_id: int):
    if not cache.add(f"org-throttle-{organization_id}", True):
        return  # Recent check already performed

    org = Organization.objects.with_event_counts().get(id=organization_id)
    _check_and_update_throttle(org)


@shared_task
def check_all_organizations_throttle():
    for org in Organization.objects.with_event_counts().iterator():
        _check_and_update_throttle(org)


def _check_and_update_throttle(org: Organization):
    from djstripe.models import Product

    plan_events: int | None = (
        Product.objects.filter(
            plan__subscriptions__customer__subscriber=org,
            plan__subscriptions__status="active",
        )
        .values_list("metadata__events", flat=True)
        .first()
    )
    if plan_events:
        plan_events = int(plan_events)
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
        org.save(update_fields=["event_throttle_rate"])
        if org_throttle > old_throttle:
            send_throttle_email.delay(org.id)


@shared_task
def send_throttle_email(organization_id: int):
    MetQuotaEmail(pk=organization_id).send_email()


@shared_task
def send_email_invite(org_user_id: int, token: str):
    InvitationEmail(pk=org_user_id, token=token).send_email()

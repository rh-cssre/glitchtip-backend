from django.conf import settings
from celery import shared_task
from .models import Organization
from .email import MetQuotaEmail, InvitationEmail


def get_free_tier_organizations_with_event_count():
    return Organization.objects.with_event_counts().filter(
        djstripe_customers__subscriptions__plan__amount=0,
        djstripe_customers__subscriptions__status="active",
    )


@shared_task
def set_organization_throttle():
    """Determine if organization should be throttled"""
    # Currently throttling only happens if billing is enabled and user has free plan.
    if settings.BILLING_ENABLED:
        events_max = settings.BILLING_FREE_TIER_EVENTS
        free_tier_organizations = get_free_tier_organizations_with_event_count()

        orgs_over_quota = free_tier_organizations.filter(
            is_accepting_events=True, total_event_count__gt=events_max
        ).select_related("owner__organization_user")
        for org in orgs_over_quota:
            send_email_met_quota.delay(org.pk)
        orgs_over_quota.update(is_accepting_events=False)

        free_tier_organizations.filter(
            is_accepting_events=False, total_event_count__lte=events_max
        ).update(is_accepting_events=True)

        # paid accounts should always be active at this time
        Organization.objects.filter(
            is_accepting_events=False,
            djstripe_customers__subscriptions__plan__amount__gt=0,
            djstripe_customers__subscriptions__status="active",
        ).update(is_accepting_events=True)


@shared_task
def send_email_met_quota(organization_id: int):
    MetQuotaEmail(pk=organization_id).send_email()


@shared_task
def send_email_invite(org_user_id: int, token: str):
    InvitationEmail(pk=org_user_id, token=token).send_email()

from django.conf import settings
from django.db.models import Count, Q, F
from celery import shared_task
from .models import Organization
from .email import send_email_met_quota


@shared_task
def set_organization_throttle():
    """ Determine if organization should be throttled """
    # Currently throttling only happens if billing is enabled and user has free plan.
    if settings.BILLING_ENABLED:
        events_max = settings.BILLING_FREE_TIER_EVENTS
        free_tier_organizations = Organization.objects.filter(
            djstripe_customers__subscriptions__plan__amount=0,
            djstripe_customers__subscriptions__status="active",
        ).annotate(
            event_count=Count(
                "projects__issue__event",
                filter=Q(
                    projects__issue__event__created__gte=F(
                        "djstripe_customers__subscriptions__current_period_start"
                    )
                ),
            )
        )

        orgs_over_quota = free_tier_organizations.filter(
            is_accepting_events=True, event_count__gt=events_max
        ).select_related("owner__organization_user")
        for org in orgs_over_quota:
            send_email_met_quota(org)
        orgs_over_quota.update(is_accepting_events=False)

        free_tier_organizations.filter(
            is_accepting_events=False, event_count__lte=events_max
        ).update(is_accepting_events=True)

        # paid accounts should always be active at this time
        Organization.objects.filter(
            is_accepting_events=False,
            djstripe_customers__subscriptions__plan__amount__gt=0,
            djstripe_customers__subscriptions__status="active",
        ).update(is_accepting_events=True)

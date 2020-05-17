from django.conf import settings
from django.utils import timezone
from django.db.models import Count, Q
from dateutil.relativedelta import relativedelta
from celery import shared_task
from .models import Organization


@shared_task
def set_organization_throttle():
    """ Determine if organization should be throttled """
    # Currently throttling only happens if billing is enabled and user has no stripe account.
    if settings.BILLING_ENABLED:
        # Throttle range is 1 month (not 30 days)
        month_ago = timezone.now() + relativedelta(months=-1)
        events_max = settings.BILLING_FREE_TIER_EVENTS
        non_subscriber_organizations = Organization.objects.filter(
            djstripe_customers__isnull=True,
        ).annotate(
            event_count=Count("projects__issue__event", filter=Q(created__gt=month_ago))
        )

        non_subscriber_organizations.filter(
            is_accepting_events=True, event_count__gt=events_max
        ).update(is_accepting_events=False)
        non_subscriber_organizations.filter(
            is_accepting_events=False, event_count__lte=events_max
        ).update(is_accepting_events=True)

        # is_accepting_events is essentially cache and cache invalidation is hard
        Organization.objects.filter(
            is_accepting_events=False, djstripe_customers__isnull=False
        ).update(is_accepting_events=True)

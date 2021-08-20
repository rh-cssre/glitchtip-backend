from django.conf import settings
from django.db.models import Count, Q, Subquery, OuterRef
from celery import shared_task
from projects.models import Project
from .models import Organization
from .email import MetQuotaEmail, InvitationEmail


def get_free_tier_organizations_with_event_count():
    queryset = Organization.objects.filter(
        djstripe_customers__subscriptions__plan__amount=0,
        djstripe_customers__subscriptions__status="active",
    )

    projects = Project.objects.filter(organization=OuterRef("pk")).values(
        "organization"
    )
    total_issue_events = projects.annotate(
        total=Count(
            "issue__event",
            filter=Q(
                issue__event__created__gte=OuterRef(
                    "djstripe_customers__subscriptions__current_period_start"
                )
            ),
        )
    ).values("total")
    total_transaction_events = projects.annotate(
        total=Count(
            "transactionevent",
            filter=Q(
                transactionevent__created__gte=OuterRef(
                    "djstripe_customers__subscriptions__current_period_start"
                )
            ),
        )
    ).values("total")

    return queryset.annotate(
        event_count=Subquery(total_issue_events) + Subquery(total_transaction_events)
    )


@shared_task
def set_organization_throttle():
    """ Determine if organization should be throttled """
    # Currently throttling only happens if billing is enabled and user has free plan.
    if settings.BILLING_ENABLED:
        events_max = settings.BILLING_FREE_TIER_EVENTS
        free_tier_organizations = get_free_tier_organizations_with_event_count()

        orgs_over_quota = free_tier_organizations.filter(
            is_accepting_events=True, event_count__gt=events_max
        ).select_related("owner__organization_user")
        for org in orgs_over_quota:
            send_email_met_quota.delay(org.pk)
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


@shared_task
def send_email_met_quota(organization_id: int):
    MetQuotaEmail(pk=organization_id).send_email()


@shared_task
def send_email_invite(org_user_id: int, token: str):
    InvitationEmail(pk=org_user_id, token=token).send_email()

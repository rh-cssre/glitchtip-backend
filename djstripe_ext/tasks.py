from django.conf import settings
from django.db.models import Count, Q, F, Subquery, OuterRef, IntegerField
from django.db.models.functions import Cast
from django.contrib.postgres.fields.jsonb import KeyTextTransform
from celery import shared_task
from organizations_ext.models import Organization
from projects.models import Project
from .models import SubscriptionQuotaWarning
from .email import send_email_warn_quota


@shared_task
def warn_organization_throttle():
    """ Warn user about approaching 80% of allotted events """
    if not settings.BILLING_ENABLED:
        return

    queryset = Organization.objects.filter(
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

    queryset = queryset.annotate(
        event_count=Subquery(total_issue_events) + Subquery(total_transaction_events),
        plan_event_count=Cast(
            KeyTextTransform(
                "events", "djstripe_customers__subscriptions__plan__product__metadata"
            ),
            output_field=IntegerField(),
        ),
    )

    # 80% to 100% of event quota
    queryset = queryset.filter(
        event_count__gte=F("plan_event_count") * 0.80,
        event_count__lte=F("plan_event_count"),
    )

    for org in queryset:
        subscription = org.djstripe_customers.first().subscription
        send_email_warn_quota(
            org, subscription, org.event_count, org.plan_event_count,
        )
        warning, created = SubscriptionQuotaWarning.objects.get_or_create(
            subscription=subscription
        )
        if not created:
            warning.save()


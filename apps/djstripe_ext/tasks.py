from celery import shared_task
from django.conf import settings
from django.db.models import F, IntegerField, Q
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast

from apps.organizations_ext.models import Organization

from .email import WarnQuotaEmail
from .models import SubscriptionQuotaWarning


@shared_task
def warn_organization_throttle():
    """Warn user about approaching 80% of allotted events"""
    if not settings.BILLING_ENABLED:
        return

    queryset = (
        Organization.objects.with_event_counts()
        .filter(
            djstripe_customers__subscriptions__status="active",
        )
        .filter(
            Q(djstripe_customers__subscriptions__subscriptionquotawarning=None)
            | Q(
                djstripe_customers__subscriptions__subscriptionquotawarning__notice_last_sent__lt=F(
                    "djstripe_customers__subscriptions__current_period_start"
                ),
            )
        )
    )

    queryset = queryset.annotate(
        plan_event_count=Cast(
            KeyTextTransform(
                "events", "djstripe_customers__subscriptions__plan__product__metadata"
            ),
            output_field=IntegerField(),
        ),
    )

    # 80% to 100% of event quota
    queryset = queryset.filter(
        total_event_count__gte=F("plan_event_count") * 0.80,
        total_event_count__lte=F("plan_event_count"),
    )

    for org in queryset:
        subscription = org.djstripe_customers.first().subscription
        send_email_warn_quota.delay(subscription.pk, org.total_event_count)
        warning, created = SubscriptionQuotaWarning.objects.get_or_create(
            subscription=subscription
        )
        if not created:
            warning.save()


@shared_task
def send_email_warn_quota(subscription_id: int, event_count: int):
    WarnQuotaEmail(pk=subscription_id, event_count=event_count).send_email()

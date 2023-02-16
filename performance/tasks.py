from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils.timezone import now

from .models import Span, TransactionEvent, TransactionGroup


@shared_task
def cleanup_old_transaction_events():
    """Delete older events and associated data"""
    days = settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS
    days_ago = now() - timedelta(days=days)

    qs = Span.objects.filter(created__lt=days_ago)
    # Fast bulk delete - see https://code.djangoproject.com/ticket/9519
    qs._raw_delete(qs.db)

    qs = TransactionEvent.objects.filter(created__lt=days_ago)
    qs._raw_delete(qs.db)

    # Delete ~1k empty transaction groups at a time until less than 1k remain then delete the rest. Avoids memory overload.
    queryset = TransactionGroup.objects.filter(transactionevent=None).order_by("id")

    while True:
        try:
            empty_group_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            queryset.filter(id__lte=empty_group_delimiter).delete()
        except TransactionGroup.DoesNotExist:
            break

    queryset.delete()

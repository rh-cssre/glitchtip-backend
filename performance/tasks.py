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
    while True:
        try:
            empty_group_delimiter = (
                TransactionGroup.objects.filter(transactionevent=None)
                .values_list("id", flat=True)[1000:1001]
                .get()
            )
            TransactionGroup.objects.filter(
                transactionevent=None, id__lte=empty_group_delimiter
            ).delete()
        except TransactionGroup.DoesNotExist:
            break

    TransactionGroup.objects.filter(transactionevent=None).delete()

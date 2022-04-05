from datetime import timedelta
from django.utils.timezone import now
from django.conf import settings
from celery import shared_task
from .models import TransactionEvent, TransactionGroup, Span


@shared_task
def cleanup_old_transaction_events():
    """Delete older events and associated data"""
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS
    qs = TransactionEvent.objects.filter(created__lt=now() - timedelta(days=days))
    # Fast bulk delete - see https://code.djangoproject.com/ticket/9519
    qs._raw_delete(qs.db)

    qs = Span.objects.filter(created__lt=now() - timedelta(days=days))
    qs._raw_delete(qs.db)

    TransactionGroup.objects.filter(transactionevent=None).delete()

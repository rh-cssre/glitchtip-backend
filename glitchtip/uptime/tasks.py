import asyncio
from datetime import timedelta
from typing import List

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import DateTimeField, ExpressionWrapper, F, OuterRef, Q, Subquery
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from alerts.models import AlertRecipient

from .email import MonitorEmail
from .models import Monitor, MonitorCheck
from .utils import fetch_all
from .webhooks import send_uptime_as_webhook


@shared_task
def dispatch_checks():
    """
    dispatch monitor checks tasks in batches, include start time for check
    """
    now = timezone.now()
    latest_check = Subquery(
        MonitorCheck.objects.filter(monitor_id=OuterRef("id"))
        .order_by("-start_check")
        .values("start_check")[:1]
    )
    # Use of atomic solves iterator() with pgbouncer InvalidCursorName issue
    # https://docs.djangoproject.com/en/3.2/ref/databases/#transaction-pooling-server-side-cursors
    with transaction.atomic():
        monitor_ids = (
            Monitor.objects.filter(organization__is_accepting_events=True)
            .annotate(
                last_min_check=ExpressionWrapper(
                    now - F("interval"), output_field=DateTimeField()
                ),
                latest_check=latest_check,
            )
            .filter(Q(latest_check__lte=F("last_min_check")) | Q(latest_check=None))
            .values_list("id", flat=True)
        )
        batch_size = 100
        batch_ids = []
        for i, monitor_id in enumerate(monitor_ids.iterator(), 1):
            batch_ids.append(monitor_id)
            if i % batch_size == 0:
                perform_checks.apply_async(args=(batch_ids, now), expires=60)
                batch_ids = []
        if len(batch_ids) > 0:
            perform_checks.delay(batch_ids, now)


@shared_task
def perform_checks(monitor_ids: List[int], now=None):
    """
    Performant check monitors and save results

    1. Fetch all monitor data for ids
    2. Async perform all checks
    3. Save in bulk results
    """
    if now is None:
        now = timezone.now()
    # Convert queryset to raw list[dict] for asyncio operations
    monitors = list(
        Monitor.objects.with_check_annotations().filter(pk__in=monitor_ids).values()
    )
    results = asyncio.run(fetch_all(monitors))
    monitor_checks = MonitorCheck.objects.bulk_create(
        [
            MonitorCheck(
                monitor_id=result["id"],
                is_up=result["is_up"],
                start_check=now,
                reason=result.get("reason", None),
                response_time=result.get("response_time", None),
            )
            for result in results
        ]
    )
    for i, result in enumerate(results):
        if result["latest_is_up"] is True and result["is_up"] is False:
            send_monitor_notification.delay(
                monitor_checks[i].pk, True, result["last_change"]
            )
        elif result["latest_is_up"] is False and result["is_up"] is True:
            send_monitor_notification.delay(
                monitor_checks[i].pk, False, result["last_change"]
            )


@shared_task
def send_monitor_notification(monitor_check_id: int, went_down: bool, last_change: str):
    recipients = AlertRecipient.objects.filter(
        alert__project__monitor__checks=monitor_check_id, alert__uptime=True
    )
    for recipient in recipients:
        if recipient.recipient_type == AlertRecipient.RecipientType.EMAIL:
            MonitorEmail(
                pk=monitor_check_id,
                went_down=went_down,
                last_change=parse_datetime(last_change) if last_change else None,
            ).send_users_email()
        elif recipient.recipient_type == AlertRecipient.RecipientType.WEBHOOK:
            send_uptime_as_webhook(
                recipient.url, monitor_check_id, went_down, last_change
            )


@shared_task
def cleanup_old_monitor_checks():
    """Delete older checks and associated data"""
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS
    qs = MonitorCheck.objects.filter(created__lt=timezone.now() - timedelta(days=days))
    # pylint: disable=protected-access
    qs._raw_delete(qs.db)  # noqa

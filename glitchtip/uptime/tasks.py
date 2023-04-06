import asyncio
from datetime import timedelta
from typing import List

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import DateTimeField, ExpressionWrapper, F, OuterRef, Q, Subquery
from django.db.models.expressions import Func
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_redis import get_redis_connection

from alerts.models import AlertRecipient

from .email import MonitorEmail
from .models import Monitor, MonitorCheck
from .utils import fetch_all
from .webhooks import send_uptime_as_webhook

UPTIME_COUNTER_KEY = "uptime_counter"
UPTIME_TICK_EXPIRE = 2147483647
UPTIME_CHECK_INTERVAL = settings.UPTIME_CHECK_INTERVAL


class Epoch(Func):
    template = "EXTRACT(epoch FROM %(expressions)s)::INTEGER"
    output_field = models.IntegerField()


def bucket_monitors(monitors, tick: int, check_interval=UPTIME_CHECK_INTERVAL):
    """
    Sort monitors into buckets based on:

    Each interval group.
    <30 seconds timeout vs >= 30 (potentially slow)

    Example: if there is one monior with interval of 1 and the check interval is 10,
    this monitor should run every time. The return should be a list of 10 ticks with
    the same monitor in each

    Result:
    {1, {True: [monitor, monitor]}}
    {1, {False: [monitor]}}
    {2, {True: [monitor]}}
    """
    result = []
    fast_monitors = [monitor for monitor in monitors if (monitor.int_timeout < 30)]
    slow_monitors = [monitor for monitor in monitors if (monitor.int_timeout >= 30)]
    result = {}
    for i in range(tick, tick + check_interval):
        fast_tick_monitors = [
            monitor
            for monitor in monitors
            if tick % monitor.interval.seconds == 0 and monitor.int_timeout < 30
        ]
        slow_tick_monitors = [
            monitor
            for monitor in monitors
            if tick % monitor.interval.seconds == 0 and monitor.int_timeout >= 30
        ]
        if fast_monitors or slow_monitors:
            result[i] = {}
            if fast_monitors:
                result[i][True] = fast_monitors
            if slow_monitors:
                result[i][False] = slow_monitors
    return result


@shared_task()
def dispatch_checks():
    """
    dispatch monitor checks tasks in batches, include start time for check
    """
    now = timezone.now()
    try:
        with get_redis_connection() as con:
            tick = con.incr(UPTIME_COUNTER_KEY)
            if tick >= UPTIME_TICK_EXPIRE:
                con.delete(UPTIME_COUNTER_KEY)
    except NotImplementedError:
        cache.add(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)
        tick = cache.incr(UPTIME_COUNTER_KEY)
    # Use of atomic solves iterator() with pgbouncer InvalidCursorName issue
    # https://docs.djangoproject.com/en/3.2/ref/databases/#transaction-pooling-server-side-cursors
    with transaction.atomic():
        monitor_ids = (
            Monitor.objects.filter(organization__is_accepting_events=True)
            .annotate(mod=tick % Epoch(F("interval")))
            .filter(mod__lt=UPTIME_CHECK_INTERVAL)
            .values_list("id", "interval", "timeout")
        )
        batch_size = 100
        batch_ids = []
        for i, monitor_id in enumerate(monitor_ids.iterator(chunk_size=10), 1):
            batch_ids.append(monitor_id)
            if i % batch_size == 0:
                # perform_checks.apply_async(args=(batch_ids, now), expires=60)
                batch_ids = []
        # if len(batch_ids) > 0:
        # perform_checks.delay(batch_ids, now)


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

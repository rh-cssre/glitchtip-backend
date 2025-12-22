import asyncio
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone
from django_valkey import get_valkey_connection

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient

from .email import MonitorEmail
from .models import Monitor, MonitorCheck, MonitorType
from .utils import fetch_all
from .webhooks import send_uptime_as_webhook

logger = logging.getLogger(__name__)

UPTIME_COUNTER_KEY = "uptime_counter"
UPTIME_TICK_EXPIRE = 2147483647
UPTIME_CHECK_INTERVAL = settings.UPTIME_CHECK_INTERVAL


def bucket_monitors(monitors, tick: int, check_interval=UPTIME_CHECK_INTERVAL):
    """
    Sort monitors into buckets based on:

    Each interval group.
    <30 seconds timeout vs >= 30 (potentially slow)

    Example: if there is one monior with interval of 1 and the check interval is 10,
    this monitor should run every time. The return should be a list of 10 ticks with
    the same monitor in each

    Result:
    {tick: {is_fast: monitors[]}}
    {1, {True: [monitor, monitor]}}
    {1, {False: [monitor]}}
    {2, {True: [monitor]}}
    """
    result = {}
    for i in range(tick, tick + check_interval):
        fast_tick_monitors = [
            monitor
            for monitor in monitors
            if i % monitor.interval == 0 and monitor.int_timeout < 30
        ]
        slow_tick_monitors = [
            monitor
            for monitor in monitors
            if i % monitor.interval == 0 and monitor.int_timeout >= 30
        ]
        if fast_tick_monitors or slow_tick_monitors:
            result[i] = {}
            if fast_tick_monitors:
                result[i][True] = fast_tick_monitors
            if slow_tick_monitors:
                result[i][False] = slow_tick_monitors
    return result


@shared_task()
def dispatch_checks():
    """
    Dispatch monitor checks tasks in batches, include start time for check

    Track each "second tick". A tick is the number of seconds away from an arbitrary start time.
    Fetch each monitor that would need to run in the next UPTIME_CHECK_INTERVAL
    Determine when monitors need to run based on each second tick and whether it's
    timeout is fast or slow (group slow together)
    For example, if our check interval is 10 and the monitor should run every 2 seconds,
    there should be 5 checks run every other second

    This method reduces the number of necessary celery tasks and sql queries. While keeping
    the timing precise and allowing for any arbitrary interval (to the second).
    It also has no need to track state of previous checks.

    The check result DB writes are then batched for better performance.
    """
    now = timezone.now()
    try:
        with get_valkey_connection() as con:
            tick = con.incr(UPTIME_COUNTER_KEY)
            if tick >= UPTIME_TICK_EXPIRE:
                con.delete(UPTIME_COUNTER_KEY)
            elif tick % 1000 == 0:  # Set sanity check TTL
                con.expire(UPTIME_COUNTER_KEY, 86400)
    except NotImplementedError:
        cache.add(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)
        tick = cache.incr(UPTIME_COUNTER_KEY)
    tick = tick * settings.UPTIME_CHECK_INTERVAL
    monitors = (
        Monitor.objects.filter(organization__event_throttle_rate__lt=100)
        .annotate(mod=tick % F("interval"))
        .filter(mod__lt=UPTIME_CHECK_INTERVAL)
        .exclude(Q(url="") & ~Q(monitor_type=MonitorType.HEARTBEAT))
        .only("id", "interval", "timeout")
    )
    for i, (tick, bucket) in enumerate(bucket_monitors(monitors, tick).items()):
        for is_fast, monitors_to_dispatch in bucket.items():
            run_time = now + timedelta(seconds=i)
            perform_checks.apply_async(
                args=([m.pk for m in monitors_to_dispatch], run_time),
                eta=run_time,
                expires=run_time + timedelta(minutes=1),
            )


@shared_task
def perform_checks(monitor_ids: list[int], now=None):
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
    results = []
    for result in asyncio.run(fetch_all(monitors)):
        # Log and ignore exceptions
        if isinstance(result, Exception):
            logger.error("Critical monitor check failure", exc_info=result)
        # Filter out "up" heartbeats
        elif (
            result["monitor_type"] != MonitorType.HEARTBEAT or result["is_up"] is False
        ):
            results.append(result)

    monitor_checks = MonitorCheck.objects.bulk_create(
        [
            MonitorCheck(
                monitor_id=result["id"],
                is_up=result["is_up"],
                is_change=result["latest_is_up"] != result["is_up"],
                start_check=now,
                reason=result.get("reason", None),
                response_time=result.get("response_time", None),
                data=result.get("data", None),
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
        if recipient.recipient_type == RecipientType.EMAIL:
            MonitorEmail(
                pk=monitor_check_id,
                went_down=went_down,
                last_change=last_change if last_change else None,
            ).send_users_email()
        elif recipient.is_webhook:
            send_uptime_as_webhook(recipient, monitor_check_id, went_down, last_change)

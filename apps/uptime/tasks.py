import asyncio
import logging
import time

import aiohttp
from django.conf import settings
from django.core.cache import cache
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient

from .email import MonitorEmail
from .models import Monitor, MonitorCheck, MonitorType
from .utils import fetch
from .webhooks import send_uptime_as_webhook

logger = logging.getLogger(__name__)

UPTIME_COUNTER_KEY = "uptime_counter"
UPTIME_TICK_EXPIRE = 2147483647


@task
async def dispatch_checks():
    """
    Dispatch monitor checks tasks in batches.
    """
    try:
        tick = await cache.aincr(UPTIME_COUNTER_KEY)
    except ValueError:
        await cache.aset(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)
        tick = await cache.aincr(UPTIME_COUNTER_KEY)

    # Reset tick if it gets too large, but keep it monotonic
    if tick >= UPTIME_TICK_EXPIRE:
        await cache.aset(UPTIME_COUNTER_KEY, 0, UPTIME_TICK_EXPIRE)

    # Dispatch checks for monitors that are scheduled to run at this tick
    # We use the monitor ID to spread the load across the interval window
    monitors = (
        Monitor.objects.filter(organization__event_throttle_rate__lt=100)
        .annotate(mod=(tick + F("id")) % F("interval"))
        .filter(mod=0)
        .exclude(Q(url="") & ~Q(monitor_type=MonitorType.HEARTBEAT))
        .only("id", "interval", "timeout")
    )

    monitor_ids = [mid async for mid in monitors.values_list("id", flat=True)]
    if monitor_ids:
        await perform_checks.aenqueue(monitor_ids)


async def save_monitor_checks(results, now):
    """
    Bulk save monitor checks and trigger notifications.
    """
    monitor_checks = await MonitorCheck.objects.abulk_create(
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
        if result["latest_is_up"] != result["is_up"]:
            last_change = result["last_change"]
            if last_change:
                last_change = last_change.isoformat()
            await send_monitor_notification.aenqueue(
                monitor_checks[i].pk, not result["is_up"], last_change
            )


async def run_checks(monitors, now):
    async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
        tasks = [asyncio.create_task(fetch(session, m)) for m in monitors]
        pending = tasks
        buffer = []
        BATCH_SIZE = 100
        FLUSH_INTERVAL = 1.0
        last_flush = time.monotonic()

        while pending:
            # Wait for tasks to finish, but flush buffer if needed
            time_since_flush = time.monotonic() - last_flush
            timeout = max(0.1, FLUSH_INTERVAL - time_since_flush)

            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
            )

            for task in done:
                try:
                    result = task.result()
                    # Filter out "up" heartbeats
                    if (
                        result["monitor_type"] != MonitorType.HEARTBEAT
                        or result["is_up"] is False
                    ):
                        buffer.append(result)
                except Exception as e:
                    logger.error("Critical monitor check failure", exc_info=e)

            # Flush if batch full or timeout reached
            if len(buffer) >= BATCH_SIZE or (
                buffer and time.monotonic() - last_flush >= FLUSH_INTERVAL
            ):
                await save_monitor_checks(buffer, now)
                buffer = []
                last_flush = time.monotonic()

        # Final flush
        if buffer:
            await save_monitor_checks(buffer, now)


@task
async def perform_checks(monitor_ids: list[int], now: str | None = None):
    """
    Performant check monitors and save results
    """
    if now is None:
        now = timezone.now()
    else:
        now = parse_datetime(now)

    # Fetch monitors asynchronously
    # Django's values() returns a QuerySet, which is async iterable in Django 4.1+
    monitors = [
        m
        async for m in Monitor.objects.with_check_annotations()
        .filter(pk__in=monitor_ids)
        .values()
    ]

    # Run async checks with smart batching
    await run_checks(monitors, now)


@task
def send_monitor_notification(
    monitor_check_id: int, went_down: bool, last_change: str | None
):
    if last_change:
        last_change = parse_datetime(last_change)
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

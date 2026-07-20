import asyncio
import logging
import time
from collections import Counter
from uuid import UUID

import aiohttp
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import F, Q
from django.tasks import task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from apps.shared.raw_sql import execute_unnest

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


async def update_uptime_statistics(org_counts: dict[int, int], check_time):
    """
    Bulk upsert UptimeCheckHourlyStatistic for a batch of monitor checks.
    Silently skips if no partition exists for the date (e.g. old test data).
    """
    hour = check_time.replace(minute=0, second=0, microsecond=0)
    data = sorted(
        ((org_id, hour, count) for org_id, count in org_counts.items()),
        key=lambda row: row[0],
    )
    if not data:
        return

    try:
        # A single INSERT ... ON CONFLICT is atomic by itself and needs no
        # surrounding transaction, so we issue it directly without an
        # async_atomic() wrapper.
        await execute_unnest(
            "INSERT INTO uptime_uptimecheckhourlystatistic "
            "(organization_id, date, count) "
            "SELECT * FROM unnest(%s::int[], %s::timestamptz[], %s::int[]) "
            "ON CONFLICT (organization_id, date) "
            "DO UPDATE SET count = "
            "uptime_uptimecheckhourlystatistic.count + EXCLUDED.count",
            list(data),
        )
    except IntegrityError:
        logger.warning(
            "Failed to update uptime statistics for hour %s (missing partition)",
            hour,
        )


def _next_state(result):
    """
    Compute a monitor's next official up/down state from a single check result.

    The official state (``cached_is_up``) only flips to down after
    ``confirmation_threshold`` consecutive failing checks (flap damping);
    recovery to up is immediate on the first successful check. ``prev_state``
    is None until the monitor has a confirmed state.

    Returns (new_state, consecutive_down, is_change, state_changed):
    ``state_changed`` is True only when the official state actually flipped and
    drives notifications. ``is_change`` additionally covers re-establishing a
    baseline when no prior change is recorded (e.g. after partition pruning);
    it is stored on the check and updates cached_last_change but, like the
    original logic, does not by itself trigger a notification.
    """
    prev_state = result["latest_is_up"]  # None until first confirmed state
    threshold = result.get("confirmation_threshold") or 1
    if result["is_up"]:
        consecutive_down, new_state = 0, True
    else:
        # Cap at threshold: the count is only ever compared >= threshold, so
        # growing past it is meaningless and would eventually overflow the
        # smallint column on a long-running outage.
        consecutive_down = min(
            (result.get("cached_consecutive_down") or 0) + 1, threshold
        )
        new_state = False if consecutive_down >= threshold else prev_state
    state_changed = new_state is not None and new_state != prev_state
    is_change = new_state is not None and (
        state_changed or result["last_change"] is None
    )
    return new_state, consecutive_down, is_change, state_changed


async def save_monitor_checks(results, now):
    """
    Bulk save monitor checks and trigger notifications.

    The raw probe result is always recorded on ``MonitorCheck.is_up`` so the
    check history shows every blip, but the monitor's official state and the
    resulting notification are debounced via ``confirmation_threshold`` (see
    ``_next_state``).
    """
    states = [_next_state(result) for result in results]

    monitor_checks = await MonitorCheck.objects.abulk_create(
        [
            MonitorCheck(
                monitor_id=result["id"],
                organization_id=result["organization_id"],
                is_up=result["is_up"],
                is_change=is_change,
                start_check=now,
                reason=result.get("reason", None),
                response_time=result.get("response_time", None),
                data=result.get("data", None),
            )
            for result, (_, _, is_change, _) in zip(results, states)
        ]
    )

    # Bulk update cached fields on Monitor
    monitors_to_update = [
        Monitor(
            pk=result["id"],
            cached_is_up=new_state,
            cached_last_change=now if is_change else result["last_change"],
            cached_consecutive_down=consecutive_down,
        )
        for result, (new_state, consecutive_down, is_change, _) in zip(results, states)
    ]
    if monitors_to_update:
        await Monitor.objects.abulk_update(
            monitors_to_update,
            ["cached_is_up", "cached_last_change", "cached_consecutive_down"],
        )

    # Update hourly statistics
    org_counts = Counter(r["organization_id"] for r in results)
    await update_uptime_statistics(org_counts, now)

    for i, (result, (new_state, _, _, state_changed)) in enumerate(
        zip(results, states)
    ):
        if state_changed:
            last_change = result["last_change"]
            if last_change:
                last_change = last_change.isoformat()
            # Pass monitor_id and composite PK as a list of strings for JSON serializability
            monitor_check_pk = [str(monitor_checks[i].id), result["organization_id"]]
            await send_monitor_notification.aenqueue(
                result["id"], monitor_check_pk, not new_state, last_change
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
async def send_monitor_notification(
    monitor_id: int, monitor_check_pk: list, went_down: bool, last_change: str | None
):
    if last_change:
        last_change = parse_datetime(last_change)

    # Convert list back to tuple for Django lookup
    monitor_check_id = (UUID(monitor_check_pk[0]), monitor_check_pk[1])

    recipients = AlertRecipient.objects.filter(
        alert__project__monitor__id=monitor_id, alert__uptime=True
    )
    async for recipient in recipients:
        if recipient.recipient_type == RecipientType.EMAIL:
            await sync_to_async(
                MonitorEmail(
                    pk=monitor_check_id,
                    went_down=went_down,
                    last_change=last_change if last_change else None,
                ).send_users_email
            )()
        else:
            await send_uptime_as_webhook(
                recipient, monitor_check_id, went_down, last_change
            )

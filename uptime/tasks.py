import asyncio
from typing import List
from django.db.models import F, ExpressionWrapper, DateTimeField, Subquery, OuterRef
from django.utils import timezone
from celery import shared_task
from .models import Monitor, MonitorCheck
from .utils import fetch_all


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
    monitor_ids = (
        Monitor.objects.filter(organization__is_accepting_events=True)
        .annotate(
            last_min_check=ExpressionWrapper(
                now - F("interval"), output_field=DateTimeField()
            ),
            latest_check=latest_check,
        )
        .filter(latest_check__lte=F("last_min_check"))
        .values_list("id", flat=True)
    )
    batch_size = 100
    batch_ids = []
    for i, monitor_id in enumerate(monitor_ids.iterator(), 1):
        batch_ids.append(monitor_id)
        if i % batch_size == 0:
            perform_checks.delay(batch_ids, now)
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
    monitors = list(Monitor.objects.filter(pk__in=monitor_ids).values())
    loop = asyncio.get_event_loop()
    results = loop.run_until_complete(fetch_all(monitors, loop))
    MonitorCheck.objects.bulk_create(
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

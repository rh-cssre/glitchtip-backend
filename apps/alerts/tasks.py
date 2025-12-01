from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from django_valkey import get_valkey_connection

from apps.issue_events.models import Issue

from .constants import ISSUE_IDS_KEY
from .models import Notification, ProjectAlert

# Lua script for atomic smembers + del
LUA_SCRIPT = """
local members = redis.call('SMEMBERS', KEYS[1])
redis.call('DEL', KEYS[1])
return members
"""


def process_alert(project_alert_id: int, issue_ids: list[int]):
    notification = Notification.objects.create(project_alert_id=project_alert_id)
    notification.issues.add(*issue_ids)
    send_notification.delay(notification.pk)


@shared_task
def process_event_alerts():
    """Inspect alerts and determine if new notifications need sent"""
    now = timezone.now()

    issue_ids: list[int] | None = None
    # Support not having valkey, in theory
    if settings.CACHE_IS_VALKEY:
        # Note all recent issue_ids at ingest time. Then we can filter by them here.
        issue_ids = [
            int(x)
            for x in get_valkey_connection("default").eval(LUA_SCRIPT, 1, ISSUE_IDS_KEY)
        ]

    project_alerts = ProjectAlert.objects.filter(
        quantity__isnull=False, timespan_minutes__isnull=False
    )
    if issue_ids == []:
        return  # There are no new issues, no work to do

    if issue_ids:
        project_alerts = project_alerts.filter(
            project__issues__id__in=issue_ids
        ).distinct()

    for alert in project_alerts:
        start_time = now - timedelta(minutes=alert.timespan_minutes)
        quantity_in_timespan = alert.quantity
        issues = (
            Issue.objects.filter(
                project_id=alert.project_id,
                issueevent__received__gte=start_time,
            )
            .exclude(notification__project_alert=alert)
            .annotate(num_events=Count("issueevent"))
            .filter(num_events__gte=quantity_in_timespan)
        )
        if issue_ids:
            issues = issues.filter(id__in=issue_ids)
        if issues:
            notification = alert.notification_set.create()
            notification.issues.add(*issues)
            send_notification.delay(notification.pk)


@shared_task
def send_notification(notification_id: int):
    notification = Notification.objects.get(pk=notification_id)
    notification.send_notifications()

from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.tasks import task
from django.utils import timezone
from django_valkey import get_valkey_connection

from apps.issue_events.models import Issue
from glitchtip.partition_manager import UUID7Helper

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
    send_notification.enqueue(notification.pk)


@task
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
    ).select_related("project")
    if issue_ids == []:
        return  # There are no new issues, no work to do

    if issue_ids:
        project_alerts = project_alerts.filter(
            project__issues__id__in=issue_ids
        ).distinct()

    for alert in project_alerts:
        start_time = now - timedelta(minutes=alert.timespan_minutes)
        # Pruning partition optimization
        start_uuid = UUID7Helper._uuid7_for_timestamp(start_time, min_random=True)
        quantity_in_timespan = alert.quantity
        issues = (
            Issue.objects.filter(
                project_id=alert.project_id,
                # Filter by UUIDv7 which encodes timestamp (enables partition pruning)
                issueevent__id__gte=start_uuid,
                issueevent__organization_id=alert.project.organization_id,
            )
            .exclude(notification__project_alert=alert)
            .annotate(num_events=Count("issueevent"))
            .filter(num_events__gte=quantity_in_timespan)
            .values_list("id", flat=True)
        )
        if issue_ids:
            issues = issues.filter(id__in=issue_ids)
        if issues:
            notification = alert.notification_set.create()
            notification.issues.add(*issues)
            send_notification.enqueue(notification.pk)


@task
def send_notification(notification_id: int):
    notification = Notification.objects.get(pk=notification_id)
    notification.send_notifications()

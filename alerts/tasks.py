from typing import List

from celery import shared_task
from django.db.models import Count, DurationField, F, Func, Q
from django.utils import timezone

from issues.models import Issue

from .models import Notification


def process_alert(project_alert_id: int, issue_ids: List[int]):
    notification = Notification.objects.create(project_alert_id=project_alert_id)
    notification.issues.add(*issue_ids)
    send_notification.delay(notification.pk)


@shared_task
def process_event_alerts():
    """Inspect alerts and determine if new notifications need sent"""
    now = timezone.now()
    issues = (
        Issue.objects.filter(
            project__projectalert__quantity__isnull=False,
            project__projectalert__timespan_minutes__isnull=False,
            notification__isnull=True,
        )
        .annotate(
            num_events=Count(
                "event",
                filter=Q(
                    event__created__gte=now
                    - Func(
                        0,
                        0,
                        0,
                        0,
                        0,
                        F("project__projectalert__timespan_minutes"),
                        function="make_interval",
                        output_field=DurationField(),
                    )
                ),
            ),
        )
        .filter(num_events__gte=F("project__projectalert__quantity"))
        .order_by("project__projectalert")
        .values("pk", "project__projectalert__id")
    )
    project_alert_id = None
    for issue in issues:
        if issue["project__projectalert__id"] != project_alert_id:
            if project_alert_id:  # If not the first in loop
                process_alert(project_alert_id, issue_ids)
            project_alert_id = issue["project__projectalert__id"]
            issue_ids = []
        issue_ids.append(issue["pk"])
    if project_alert_id and issue_ids:
        process_alert(project_alert_id, issue_ids)


@shared_task
def send_notification(notification_id: int):
    notification = Notification.objects.get(pk=notification_id)
    notification.send_notifications()

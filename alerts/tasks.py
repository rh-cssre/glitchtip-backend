from datetime import timedelta

from celery import shared_task
from django.db.models import Count
from django.utils import timezone

from issues.models import Issue

from .models import Notification, ProjectAlert


def process_alert(project_alert_id: int, issue_ids: list[int]):
    notification = Notification.objects.create(project_alert_id=project_alert_id)
    notification.issues.add(*issue_ids)
    send_notification.delay(notification.pk)


@shared_task
def process_event_alerts():
    """Inspect alerts and determine if new notifications need sent"""
    now = timezone.now()
    for alert in ProjectAlert.objects.filter(
        quantity__isnull=False, timespan_minutes__isnull=False
    ):
        start_time = now - timedelta(minutes=alert.timespan_minutes)
        quantity_in_timespan = alert.quantity
        issues = (
            Issue.objects.filter(
                project_id=alert.project_id,
                notification__isnull=True,
                event__created__gte=start_time,
            )
            .annotate(num_events=Count("event"))
            .filter(num_events__gte=quantity_in_timespan)
        )
        if issues:
            notification = alert.notification_set.create()
            notification.issues.add(*issues)
            send_notification.delay(notification.pk)


@shared_task
def send_notification(notification_id: int):
    notification = Notification.objects.get(pk=notification_id)
    notification.send_notifications()

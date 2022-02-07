from datetime import timedelta
from django.db.models import Count
from django.utils import timezone
from celery import shared_task
from projects.models import Project
from .models import Notification


@shared_task
def process_event_alerts():
    """ Inspect alerts and determine if new notifications need sent """
    now = timezone.now()
    for project in Project.objects.all():
        for alert in project.projectalert_set.filter(
            quantity__isnull=False, timespan_minutes__isnull=False
        ):
            start_time = now - timedelta(minutes=alert.timespan_minutes)
            quantity_in_timespan = alert.quantity
            issues = (
                project.issue_set.filter(
                    notification__isnull=True, event__created__gte=start_time,
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

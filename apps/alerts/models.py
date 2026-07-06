import asyncio

from asgiref.sync import sync_to_async
from django.contrib.postgres.fields import ArrayField
from django.db import models

from glitchtip.base_models import CreatedModel

from .constants import RecipientType
from .email import send_email_notification
from .webhooks import send_webhook_notification


class ProjectAlert(CreatedModel):
    """
    Example: Send notification when project has 15 events in 5 minutes.
    """

    name = models.CharField(max_length=255, blank=True)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    timespan_minutes = models.PositiveSmallIntegerField(blank=True, null=True)
    quantity = models.PositiveSmallIntegerField(blank=True, null=True)
    uptime = models.BooleanField(
        default=False, help_text="Send alert on any uptime monitor check failure"
    )


class AlertRecipient(models.Model):
    """An asset that accepts an alert such as email, SMS, webhooks"""

    alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    recipient_type = models.CharField(max_length=16, choices=RecipientType.choices)
    url = models.URLField(max_length=2000, blank=True)
    config = models.JSONField(default=dict, blank=True)
    tags_to_add = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        null=True,
        help_text="List of additional tags to include in the alert",
    )

    class Meta:
        unique_together = ("alert", "recipient_type", "url")

    async def send(self, notification):
        if self.recipient_type == RecipientType.EMAIL:
            await sync_to_async(send_email_notification)(notification)
        else:
            await send_webhook_notification(
                notification,
                self.url,
                self.recipient_type,
                tags_to_add=self.tags_to_add,
                config=self.config,
            )


class Notification(CreatedModel):
    project_alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    is_sent = models.BooleanField(default=False)
    issues = models.ManyToManyField("issue_events.Issue")

    async def send_notifications(self):
        recipients = [
            recipient async for recipient in self.project_alert.alertrecipient_set.all()
        ]
        if recipients:
            # Each recipient is an independent outbound webhook/email POST with
            # no data dependency between them, so fan them out concurrently
            # rather than paying the sum of their round-trips. return_exceptions
            # keeps one slow or failing destination from blocking the others
            # (individual webhook senders already swallow timeouts/client errors).
            await asyncio.gather(
                *(recipient.send(self) for recipient in recipients),
                return_exceptions=True,
            )
        else:
            await sync_to_async(send_email_notification)(self)
        self.is_sent = True
        await self.asave()

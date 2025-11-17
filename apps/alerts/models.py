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
    tags_to_add = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
        null=True,
        help_text="List of additional tags to include in the alert",
    )

    class Meta:
        unique_together = ("alert", "recipient_type", "url")

    @property
    def is_webhook(self):
        return self.recipient_type in (
            RecipientType.DISCORD,
            RecipientType.GENERAL_WEBHOOK,
            RecipientType.GOOGLE_CHAT,
        )

    def send(self, notification):
        if self.recipient_type == RecipientType.EMAIL:
            send_email_notification(notification)
        elif self.is_webhook:
            send_webhook_notification(
                notification,
                self.url,
                self.recipient_type,
                tags_to_add=self.tags_to_add,
            )


class Notification(CreatedModel):
    project_alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    is_sent = models.BooleanField(default=False)
    issues = models.ManyToManyField("issue_events.Issue")

    def send_notifications(self):
        for recipient in self.project_alert.alertrecipient_set.all():
            recipient.send(self)
        # Temp backwards compat hack - no recipients means not set up yet
        if self.project_alert.alertrecipient_set.all().exists() is False:
            send_email_notification(self)
        self.is_sent = True
        self.save()

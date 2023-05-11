from django.db import models
from django.utils.translation import gettext_lazy as _

from glitchtip.base_models import CreatedModel

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

    class RecipientType(models.TextChoices):
        EMAIL = "email", _("Email")
        WEBHOOK = "webhook", _("Webhook")

    class WebhookType(models.TextChoices):
        DISCORD = "discord", _("Discord")
        SLACK = "slack", _("Slack")
        MICROSOFT_TEAMS = "microsoft_teams", _("Microsoft Teams")
        ROCKET_CHAT = "rocket_chat", _("Rocket.Chat")
        GENERAL = "general_webhook", _("General Webhook") # Backwards Compatibility

    alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    recipient_type = models.CharField(max_length=16, choices=RecipientType.choices)
    webhook_type = models.CharField(max_length=255, default=WebhookType.GENERAL, choices=WebhookType.choices)
    url = models.URLField(max_length=2000, blank=True)

    class Meta:
        unique_together = ("alert", "recipient_type", "url")

    def send(self, notification):
        if self.recipient_type == self.RecipientType.EMAIL:
            send_email_notification(notification)
        elif self.recipient_type == self.RecipientType.WEBHOOK:
            send_webhook_notification(notification, self.url, self.webhook_type)


class Notification(CreatedModel):
    project_alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    is_sent = models.BooleanField(default=False)
    issues = models.ManyToManyField("issues.Issue")


    def send_notifications(self):
        for recipient in self.project_alert.alertrecipient_set.all():
            recipient.send(self)
        # Temp backwards compat hack - no recipients means not set up yet
        if self.project_alert.alertrecipient_set.all().exists() is False:
            send_email_notification(self)
        self.is_sent = True
        self.save()

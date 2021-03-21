from django.db import models
from django.utils.translation import gettext_lazy as _
from .email import send_email_notification


class Notification(models.Model):
    created = models.DateField(auto_now_add=True)
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    is_sent = models.BooleanField(default=False)
    issues = models.ManyToManyField("issues.Issue")

    def send_notifications(self):
        """ Email only for now, eventually needs to be an extendable system """
        send_email_notification(self)
        self.is_sent = True
        self.save()


class ProjectAlert(models.Model):
    """
    Example: Send notification when project has 15 events in 5 minutes.
    """

    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    timespan_minutes = models.PositiveSmallIntegerField(blank=True, null=True)
    quantity = models.PositiveSmallIntegerField(blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)


class AlertRecipient(models.Model):
    """ An asset that accepts an alert such as email, SMS, webhooks """

    class RecipientType(models.TextChoices):
        EMAIL = "email", _("Email")
        WEBHOOK = "webhook", _("Webhook")

    alert = models.ForeignKey(ProjectAlert, on_delete=models.CASCADE)
    recipient_type = models.CharField(max_length=16, choices=RecipientType.choices)

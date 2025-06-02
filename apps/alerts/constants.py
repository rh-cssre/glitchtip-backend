from django.db import models
from django.utils.translation import gettext_lazy as _

ISSUE_IDS_KEY = "alert_issue_ids"


class RecipientType(models.TextChoices):
    EMAIL = "email", _("Email")
    GENERAL_WEBHOOK = "webhook", _("General Slack-compatible webhook")
    DISCORD = "discord", _("Discord")
    GOOGLE_CHAT = "googlechat", _("Google Chat webhook")

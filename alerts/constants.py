from django.db import models
from django.utils.translation import gettext_lazy as _


class RecipientType(models.TextChoices):
    EMAIL = "email", _("Email")
    GENERAL_WEBHOOK = "webhook", _("General Slack-compatible webhook")
    DISCORD = "discord", _("Discord")

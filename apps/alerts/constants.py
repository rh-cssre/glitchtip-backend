from django.db import models
from django.utils.translation import gettext_lazy as _

ISSUE_IDS_KEY = "alert_issue_ids"

# Upper bound on ProjectAlert.environments. An alert naming this many
# environments is already unusual; the cap is there so an API client cannot
# grow the array (and the IN clause built from it) without limit.
MAX_ALERT_ENVIRONMENTS = 100

ENVIRONMENTS_HELP_TEXT = (
    "Only alert on events and uptime monitors from these environments. "
    "Empty matches every environment."
)


class RecipientType(models.TextChoices):
    EMAIL = "email", _("Email")
    GENERAL_WEBHOOK = (
        "webhook",
        _("Generic webhook (Mattermost, Rocket.Chat, and more)"),
    )
    DISCORD = "discord", _("Discord")
    SLACK = "slack", _("Slack")
    GOOGLE_CHAT = "googlechat", _("Google Chat webhook")
    NTFY = "ntfy", _("ntfy")
    MICROSOFT_TEAMS = "teams", _("Microsoft Teams")
    ZULIP = "zulip", _("Zulip")
    FEISHU = "feishu", _("Feishu (Lark) webhook")

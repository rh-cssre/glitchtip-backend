from datetime import datetime

from alerts.constants import RecipientType
from alerts.models import AlertRecipient
from alerts.webhooks import (
    DiscordEmbed,
    MSTeamsSection,
    WebhookAttachment,
    send_discord_webhook,
    send_webhook,
)

from .models import MonitorCheck


def send_uptime_as_webhook(
    recipient: AlertRecipient,
    monitor_check_id: int,
    went_down: bool,
    last_change: datetime,
):
    """
    Notification about uptime event via webhook.
    """
    monitor_check = MonitorCheck.objects.get(pk=monitor_check_id)
    monitor = monitor_check.monitor

    message = (
        "The monitored site has gone down."
        if went_down
        else "The monitored site is back up."
    )
    subject = "GlitchTip Uptime Alert"

    if recipient.recipient_type == RecipientType.GENERAL_WEBHOOK:
        attachment = WebhookAttachment(
            str(monitor.name), monitor.get_detail_url(), message
        )
        section = MSTeamsSection(str(monitor.name), message)
        return send_webhook(recipient.url, subject, [attachment], [section])
    elif recipient.recipient_type == RecipientType.DISCORD:
        embed = DiscordEmbed(
            title=monitor,
            description=message,
            color=None,
            fields=[],
            url=monitor.get_detail_url(),
        )
        return send_discord_webhook(recipient.url, subject, [embed])

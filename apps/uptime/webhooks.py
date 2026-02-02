from datetime import datetime
from uuid import UUID

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from apps.alerts.webhooks import (
    DiscordEmbed,
    GoogleChatCard,
    MSTeamsSection,
    WebhookAttachment,
    send_discord_webhook,
    send_googlechat_webhook,
    send_webhook,
)

from .models import MonitorCheck


def send_uptime_as_webhook(
    recipient: AlertRecipient,
    monitor_check_id: tuple | list | int,
    went_down: bool,
    last_change: datetime,
):
    """
    Notification about uptime event via webhook.
    """
    if isinstance(monitor_check_id, (list, tuple)):
        mid = (
            UUID(monitor_check_id[0])
            if isinstance(monitor_check_id[0], str)
            else monitor_check_id[0]
        )
        oid = int(monitor_check_id[1])
        monitor_check = MonitorCheck.objects.get(id=mid, organization_id=oid)
    else:
        monitor_check = MonitorCheck.objects.get(id=monitor_check_id)
    monitor = monitor_check.monitor

    message = (
        "The monitored site has gone down."
        if went_down
        else "The monitored site is back up."
    )
    subject = "GlitchTip Uptime Alert"
    title = monitor.name

    if recipient.recipient_type == RecipientType.GENERAL_WEBHOOK:
        attachment = WebhookAttachment(title, monitor.get_detail_url(), message)
        section = MSTeamsSection(str(monitor.name), message)
        return send_webhook(recipient.url, subject, [attachment], [section])
    elif recipient.recipient_type == RecipientType.GOOGLE_CHAT:
        card = GoogleChatCard().construct_uptime_card(
            title=subject,
            subtitle=title,
            text=message,
            url=monitor.get_detail_url(),
        )
        return send_googlechat_webhook(recipient.url, [card])
    elif recipient.recipient_type == RecipientType.DISCORD:
        embed = DiscordEmbed(
            title=title,
            description=message,
            color=None,
            fields=[],
            url=monitor.get_detail_url(),
        )
        return send_discord_webhook(recipient.url, subject, [embed])

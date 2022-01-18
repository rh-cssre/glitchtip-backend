from typing import List
from datetime import datetime
from alerts.webhooks import (
    WebhookAttachment,
    WebhookPayload,
    MSTeamsSection,
    send_webhook,
)
from .models import MonitorCheck


def send_uptime_as_webhook(
    url, monitor_check_id: int, went_down: bool, last_change: datetime
):
    """
    Notification about uptime event via webhook.
    """
    monitor_check = MonitorCheck.objects.get(pk=monitor_check_id)
    monitor = monitor_check.monitor

    message = str(went_down)
    attachment = WebhookAttachment(monitor, monitor.get_detail_url(), message)
    section = MSTeamsSection(str(monitor), message)

    message = "GlitchTip Uptime Alert"
    return send_webhook(url, message, [attachment], [section])

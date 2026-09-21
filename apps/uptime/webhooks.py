from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from apps.alerts.constants import RecipientType
from apps.alerts.models import AlertRecipient
from apps.alerts.webhooks import (
    SLACK_MAX_SECTION_TEXT,
    DiscordEmbed,
    GoogleChatCard,
    WebhookAttachment,
    escape_slack_mrkdwn,
    send_discord_webhook,
    send_feishu_webhook,
    send_googlechat_webhook,
    send_ntfy,
    send_slack_webhook,
    send_teams_webhook,
    send_webhook,
    send_zulip_message,
)

from .models import Monitor, MonitorCheck


@dataclass
class UptimeAlert:
    """The up/down event a handler renders.

    Carries `went_down` alongside the prose so a handler can vary its
    presentation (emoji, color) by state rather than parsing `message`.
    """

    monitor: Monitor
    subject: str
    message: str
    went_down: bool


async def _resolve_monitor_check(monitor_check_id):
    qs = MonitorCheck.objects.select_related("monitor__project__organization")
    if isinstance(monitor_check_id, (list, tuple)):
        mid = (
            UUID(monitor_check_id[0])
            if isinstance(monitor_check_id[0], str)
            else monitor_check_id[0]
        )
        oid = int(monitor_check_id[1])
        return await qs.aget(id=mid, organization_id=oid)
    return await qs.aget(id=monitor_check_id)


async def _send_uptime_generic(recipient, alert: UptimeAlert):
    attachment = WebhookAttachment(
        alert.monitor.name, alert.monitor.get_detail_url(), alert.message
    )
    return await send_webhook(recipient.url, alert.subject, [attachment])


async def _send_uptime_googlechat(recipient, alert: UptimeAlert):
    card = GoogleChatCard().construct_uptime_card(
        title=alert.subject,
        subtitle=alert.monitor.name,
        text=alert.message,
        url=alert.monitor.get_detail_url(),
    )
    return await send_googlechat_webhook(recipient.url, [card])


async def _send_uptime_discord(recipient, alert: UptimeAlert):
    embed = DiscordEmbed(
        title=alert.monitor.name,
        description=alert.message,
        color=None,
        fields=[],
        url=alert.monitor.get_detail_url(),
    )
    return await send_discord_webhook(recipient.url, alert.subject, [embed])


async def _send_uptime_ntfy(recipient, alert: UptimeAlert):
    return await send_ntfy(
        recipient.url,
        title=alert.subject,
        message=f"**{alert.monitor.name}**\n{alert.message}",
        click_url=alert.monitor.get_detail_url(),
        tags=["warning"],
    )


async def _send_uptime_teams(recipient, alert: UptimeAlert):
    body = [
        {
            "type": "TextBlock",
            "size": "Large",
            "weight": "Bolder",
            "text": alert.subject,
        },
        {"type": "TextBlock", "weight": "Bolder", "text": alert.monitor.name},
        {"type": "TextBlock", "text": alert.message, "wrap": True},
    ]
    actions = [
        {
            "type": "Action.OpenUrl",
            "title": "View Monitor",
            "url": alert.monitor.get_detail_url(),
        }
    ]
    return await send_teams_webhook(recipient.url, body, actions)


async def _send_uptime_feishu(recipient, alert: UptimeAlert):
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": alert.subject},
                "template": "red" if alert.went_down else "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{alert.monitor.name}**\n{alert.message}",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "View Monitor",
                            },
                            "url": alert.monitor.get_detail_url(),
                            "type": "primary",
                        }
                    ],
                },
            ],
        },
    }
    return await send_feishu_webhook(recipient.url, payload)


async def _send_uptime_slack(recipient, alert: UptimeAlert):
    monitor = alert.monitor
    emoji = ":rotating_light:" if alert.went_down else ":large_green_circle:"
    name = escape_slack_mrkdwn(monitor.name)
    section_text = (
        f"{emoji} *<{monitor.get_detail_url()}|{name}>*"
        f"\n{escape_slack_mrkdwn(alert.message)}"
    )
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": alert.subject}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": section_text[:SLACK_MAX_SECTION_TEXT],
            },
        },
    ]
    # Monitor.project is nullable (on_delete=SET_NULL)
    if monitor.project:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Project:* {escape_slack_mrkdwn(monitor.project.name)}",
                    }
                ],
            }
        )
    return await send_slack_webhook(
        recipient.url,
        f"{alert.subject}: {name} — {escape_slack_mrkdwn(alert.message)}",
        blocks,
    )


async def _send_uptime_zulip(recipient, alert: UptimeAlert):
    config = recipient.config or {}
    content = (
        f"## {alert.subject}\n\n**{alert.monitor.name}**\n{alert.message}\n\n"
        f"[View Monitor]({alert.monitor.get_detail_url()})"
    )
    return await send_zulip_message(
        server_url=recipient.url,
        bot_email=config.get("bot_email", ""),
        api_key=config.get("api_key", ""),
        channel=config.get("channel", ""),
        topic=config.get("topic", "GlitchTip Alerts"),
        content=content,
    )


UPTIME_NOTIFICATION_HANDLERS = {
    RecipientType.GENERAL_WEBHOOK: _send_uptime_generic,
    RecipientType.DISCORD: _send_uptime_discord,
    RecipientType.FEISHU: _send_uptime_feishu,
    RecipientType.GOOGLE_CHAT: _send_uptime_googlechat,
    RecipientType.NTFY: _send_uptime_ntfy,
    RecipientType.MICROSOFT_TEAMS: _send_uptime_teams,
    RecipientType.SLACK: _send_uptime_slack,
    RecipientType.ZULIP: _send_uptime_zulip,
}


async def send_uptime_as_webhook(
    recipient: AlertRecipient,
    monitor_check_id: tuple | list | int,
    went_down: bool,
    last_change: datetime,
):
    """
    Notification about uptime event via webhook.
    """
    monitor_check = await _resolve_monitor_check(monitor_check_id)

    alert = UptimeAlert(
        monitor=monitor_check.monitor,
        subject="GlitchTip Uptime Alert",
        message=(
            "The monitored site has gone down."
            if went_down
            else "The monitored site is back up."
        ),
        went_down=went_down,
    )

    handler = UPTIME_NOTIFICATION_HANDLERS.get(
        recipient.recipient_type, _send_uptime_generic
    )
    return await handler(recipient, alert)

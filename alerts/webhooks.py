from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, List, Optional

import requests
from django.conf import settings

from .constants import RecipientType

if TYPE_CHECKING:
    from issues.models import Issue

    from .models import Notification


@dataclass
class WebhookAttachmentField:
    title: str
    value: str
    short: bool


@dataclass
class WebhookAttachment:
    title: str
    title_link: str
    text: str
    image_url: Optional[str] = None
    color: Optional[str] = None
    fields: Optional[List[WebhookAttachmentField]] = None
    mrkdown_in: Optional[List[str]] = None


@dataclass
class MSTeamsSection:
    """
    Similar to WebhookAttachment but for MS Teams
    https://docs.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/connectors-using?tabs=cURL
    """

    activityTitle: str
    activitySubtitle: str


@dataclass
class WebhookPayload:
    alias: str
    text: str
    attachments: List[WebhookAttachment]
    sections: List[MSTeamsSection]


def send_webhook(
    url: str,
    message: str,
    attachments: Optional[List[WebhookAttachment]] = None,
    sections: Optional[List[MSTeamsSection]] = None,
):
    if not attachments:
        attachments = []
    if not sections:
        sections = []
    data = WebhookPayload(
        alias="GlitchTip", text=message, attachments=attachments, sections=sections
    )
    return requests.post(url, json=asdict(data), timeout=10)


def send_issue_as_webhook(url, issues: List["Issue"], issue_count: int = 1):
    """
    Notification about issues via webhook.
    url: Webhook URL
    issues: This should be only the issues to send as attachment
    issue_count - total issues, may be greater than len(issues)
    """
    attachments: List[WebhookAttachment] = []
    sections: List[MSTeamsSection] = []
    for issue in issues:
        fields = [
            WebhookAttachmentField(
                title="Project",
                value=issue.project.name,
                short=True,
            )
        ]
        environment = issue.tags.get("environment")
        if environment:
            fields.append(
                WebhookAttachmentField(
                    title="Environment",
                    value=environment[0],
                    short=True,
                )
            )
        release = issue.tags.get("release")
        if release:
            fields.append(
                WebhookAttachmentField(
                    title="Release",
                    value=release[0],
                    short=False,
                )
            )
        attachments.append(
            WebhookAttachment(
                mrkdown_in=["text"],
                title=str(issue),
                title_link=issue.get_detail_url(),
                text=issue.culprit,
                color=issue.get_hex_color(),
                fields=fields,
            )
        )
        sections.append(
            MSTeamsSection(
                activityTitle=str(issue),
                activitySubtitle=f"[View Issue {issue.short_id_display}]({issue.get_detail_url()})",
            )
        )
    message = "GlitchTip Alert"
    if issue_count > 1:
        message += f" ({issue_count} issues)"
    return send_webhook(url, message, attachments, sections)


@dataclass
class DiscordField:
    name: str
    value: str
    inline: bool = False


@dataclass
class DiscordEmbed:
    title: str
    description: str
    color: int
    url: str
    fields: List[DiscordField]


@dataclass
class DiscordWebhookPayload:
    content: str
    embeds: List[DiscordEmbed]


def send_issue_as_discord_webhook(url, issues: List["Issue"], issue_count: int = 1):
    embeds: List[DiscordEmbed] = []

    for issue in issues:
        fields = [
            DiscordField(
                name="Project",
                value=issue.project.name,
                inline=True,
            )
        ]
        environment = issue.tags.get("environment")
        if environment:
            fields.append(
                DiscordField(
                    name="Environment",
                    value=environment[0],
                    inline=True,
                )
            )
        release = issue.tags.get("release")
        if release:
            fields.append(
                DiscordField(
                    name="Release",
                    value=release[0],
                    inline=False,
                )
            )

        embeds.append(
            DiscordEmbed(
                title=str(issue),
                description=issue.culprit,
                color=int(issue.get_hex_color()[1:], 16)
                if issue.get_hex_color() is not None
                else None,
                url=issue.get_detail_url(),
                fields=fields,
            )
        )

    message = "GlitchTip Alert"
    if issue_count > 1:
        message += f" ({issue_count} issues)"

    return send_discord_webhook(url, message, embeds)


def send_discord_webhook(url: str, message: str, embeds: List[DiscordEmbed]):
    payload = DiscordWebhookPayload(content=message, embeds=embeds)
    return requests.post(url, json=asdict(payload), timeout=10)


def send_webhook_notification(
    notification: "Notification", url: str, recipient_type: str
):
    issue_count = notification.issues.count()
    issues = notification.issues.all()[: settings.MAX_ISSUES_PER_ALERT]

    if recipient_type == RecipientType.DISCORD:
        send_issue_as_discord_webhook(url, issues, issue_count)
    else:
        send_issue_as_webhook(url, issues, issue_count)

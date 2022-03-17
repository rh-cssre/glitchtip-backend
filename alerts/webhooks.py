from typing import List, TYPE_CHECKING, Optional
from dataclasses import dataclass, asdict
import requests


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
    attachments: List[WebhookAttachment] = [],
    sections: List[MSTeamsSection] = [],
):
    data = WebhookPayload(
        alias="GlitchTip", text=message, attachments=attachments, sections=sections
    )
    response = requests.post(url, json=asdict(data))
    return response


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


def send_webhook_notification(notification: "Notification", url: str):
    issue_count = notification.issues.count()
    issues = notification.issues.all()[:3]  # Show no more than three
    send_issue_as_webhook(url, issues, issue_count)

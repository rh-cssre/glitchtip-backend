from typing import List, TYPE_CHECKING, Optional
from dataclasses import dataclass, asdict
import requests


if TYPE_CHECKING:
    from issues.models import Issue
    from .models import Notification


@dataclass
class WebhookAttachment:
    title: str
    title_link: str
    text: str
    image_url: Optional[str] = None
    color: Optional[str] = None


@dataclass
class WebhookPayload:
    alias: str
    text: str
    attachments: List[WebhookAttachment]


def send_webhook(url: str, message: str, attachments: List[WebhookAttachment] = []):
    data = WebhookPayload(alias="GlitchTip", text=message, attachments=attachments)
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
    for issue in issues:
        attachments.append(
            WebhookAttachment(
                title=str(issue),
                title_link=issue.get_detail_url(),
                text=issue.culprit,
                color=issue.get_hex_color(),
            )
        )
    message = "GlitchTip Alert"
    if issue_count > 1:
        message += f" ({issue_count} issues)"
    return send_webhook(url, message, attachments)


def send_webhook_notification(notification: "Notification", url: str):
    issue_count = notification.issues.count()
    issues = notification.issues.all()[:3]  # Show no more than three
    send_issue_as_webhook(url, issues, issue_count)

from typing import List, TYPE_CHECKING, Optional
from dataclasses import dataclass, asdict
import requests

if TYPE_CHECKING:
    from issues.models import Issue


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
    data = WebhookPayload(alias="Glitchy", text=message, attachments=attachments)
    response = requests.post(url, json=asdict(data))
    print(response.json())


def send_issue_as_webhook(url, issue: "Issue"):
    attachment = WebhookAttachment(
        title=str(issue), title_link=issue.get_detail_url(), text=issue.culprit
    )
    message = "GlitchTip Alert"
    return send_webhook(url, message, [attachment])

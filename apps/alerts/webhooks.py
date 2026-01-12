from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from django.db.models import F
from requests.exceptions import ReadTimeout

from .constants import RecipientType

if TYPE_CHECKING:
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
    image_url: str | None = None
    color: str | None = None
    fields: list[WebhookAttachmentField] | None = None
    mrkdown_in: list[str] | None = None


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
    attachments: list[WebhookAttachment]
    sections: list[MSTeamsSection]


def send_webhook(
    url: str,
    message: str,
    attachments: list[WebhookAttachment] | None = None,
    sections: list[MSTeamsSection] | None = None,
):
    if not attachments:
        attachments = []
    if not sections:
        sections = []
    data = WebhookPayload(
        alias="GlitchTip", text=message, attachments=attachments, sections=sections
    )
    try:
        return requests.post(
            url,
            json=asdict(data),
            headers={"Content-type": "application/json"},
            timeout=10,
        )
    except ReadTimeout:
        # Ignore timeout
        return None


def send_issue_as_webhook(url, issues: list, issue_count: int = 1, **kwargs):
    """
    Notification about issues via webhook.
    url: Webhook URL
    issues: This should be only the issues to send as attachment
    issue_count - total issues, may be greater than len(issues)
    kwargs: Additional parameters
    """
    attachments: list[WebhookAttachment] = []
    sections: list[MSTeamsSection] = []
    for issue in issues:
        fields = [
            WebhookAttachmentField(
                title="Project",
                value=issue.project.name,
                short=True,
            )
        ]
        environment = (
            issue.issuetag_set.filter(tag_key__key="environment")
            .values(value=F("tag_value__value"))
            .first()
        )
        if environment:
            fields.append(
                WebhookAttachmentField(
                    title="Environment",
                    value=environment["value"],
                    short=True,
                )
            )
        server_name = (
            issue.issuetag_set.filter(tag_key__key="server_name")
            .values(value=F("tag_value__value"))
            .first()
        )
        if server_name:
            fields.append(
                WebhookAttachmentField(
                    title="Server Name",
                    value=server_name["value"],
                    short=True,
                )
            )
        release = (
            issue.issuetag_set.filter(tag_key__key="release")
            .values(value=F("tag_value__value"))
            .first()
        )
        if release:
            fields.append(
                WebhookAttachmentField(
                    title="Release",
                    value=release["value"],
                    short=False,
                )
            )

        tags_to_add = kwargs.get("tags_to_add", [])
        if tags_to_add:
            for tag in tags_to_add:
                tag_content = (
                    issue.issuetag_set.filter(tag_key__key=tag)
                    .values(value=F("tag_value__value"))
                    .first()
                )
                if tag_content:
                    fields.append(
                        WebhookAttachmentField(
                            title=tag.capitalize(),
                            value=tag_content["value"],
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
    fields: list[DiscordField]


@dataclass
class DiscordWebhookPayload:
    content: str
    embeds: list[DiscordEmbed]


def send_issue_as_discord_webhook(
    url, issues: list, issue_count: int = 1, tags_to_add: list[str] | None = None
):
    if tags_to_add is None:
        tags_to_add = []

    embeds: list[DiscordEmbed] = []

    for issue in issues:
        fields = [
            DiscordField(
                name="Project",
                value=issue.project.name,
                inline=True,
            )
        ]
        environment = (
            issue.issuetag_set.filter(tag_key__key="environment")
            .values(value=F("tag_value__value"))
            .first()
        )
        if environment:
            fields.append(
                DiscordField(
                    name="Environment",
                    value=environment["value"],
                    inline=True,
                )
            )
        release = (
            issue.issuetag_set.filter(tag_key__key="release")
            .values(value=F("tag_value__value"))
            .first()
        )
        if release:
            fields.append(
                DiscordField(
                    name="Release",
                    value=release["value"],
                    inline=False,
                )
            )
        server_name = (
            issue.issuetag_set.filter(tag_key__key="server_name")
            .values(value=F("tag_value__value"))
            .first()
        )
        if server_name:
            fields.append(
                DiscordField(
                    name="Server name",
                    value=server_name["value"],
                    inline=False,
                )
            )

        if tags_to_add:
            for tag in tags_to_add:
                tag_content = (
                    issue.issuetag_set.filter(tag_key__key=tag)
                    .values(value=F("tag_value__value"))
                    .first()
                )
                if tag_content:
                    fields.append(
                        DiscordField(
                            name=tag.capitalize(),
                            value=tag_content["value"],
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


def send_discord_webhook(url: str, message: str, embeds: list[DiscordEmbed]):
    payload = DiscordWebhookPayload(content=message, embeds=embeds)
    return requests.post(url, json=asdict(payload), timeout=10)


@dataclass
class GoogleChatCard:
    header: dict | None = None
    sections: list[dict] | None = None

    def construct_uptime_card(self, title: str, subtitle: str, text: str, url: str):
        self.header = dict(
            title=title,
            subtitle=subtitle,
        )
        self.sections = [
            dict(
                widgets=[
                    dict(
                        decoratedText=dict(
                            text=text,
                            button=dict(
                                text="View", onClick=dict(openLink=dict(url=url))
                            ),
                        )
                    )
                ]
            )
        ]
        return self

    def construct_issue_card(
        self, title: str, issue, tags_to_add: list[str] | None = None
    ):
        if tags_to_add is None:
            tags_to_add = []

        self.header = dict(title=title, subtitle=issue.project.name)
        section_header = "<font color='{}'>{}</font>".format(
            issue.get_hex_color(), str(issue)
        )
        widgets = []
        widgets.append(dict(decoratedText=dict(topLabel="Culprit", text=issue.culprit)))
        environment = (
            issue.issuetag_set.filter(tag_key__key="environment")
            .values(value=F("tag_value__value"))
            .first()
        )
        if environment:
            widgets.append(
                dict(
                    decoratedText=dict(
                        topLabel="Environment", text=environment["value"]
                    )
                )
            )
        server_name = (
            issue.issuetag_set.filter(tag_key__key="server_name")
            .values(value=F("tag_value__value"))
            .first()
        )
        if server_name:
            widgets.append(
                dict(
                    decoratedText=dict(
                        topLabel="Server Name", text=server_name["value"]
                    )
                )
            )
        release = (
            issue.issuetag_set.filter(tag_key__key="release")
            .values(value=F("tag_value__value"))
            .first()
        )
        if release:
            widgets.append(
                dict(decoratedText=dict(topLabel="Release", text=release["value"]))
            )

        if tags_to_add:
            for tag in tags_to_add:
                tag_content = (
                    issue.issuetag_set.filter(tag_key__key=tag)
                    .values(value=F("tag_value__value"))
                    .first()
                )
                if tag_content:
                    widgets.append(
                        dict(
                            decoratedText=dict(
                                topLabel=tag.capitalize(), text=tag_content["value"]
                            )
                        )
                    )

        widgets.append(
            dict(
                buttonList=dict(
                    buttons=[
                        dict(
                            text="View Issue {}".format(issue.short_id_display),
                            onClick=dict(openLink=dict(url=issue.get_detail_url())),
                        )
                    ]
                )
            )
        )
        self.sections = [dict(header=section_header, widgets=widgets)]
        return self


@dataclass
class GoogleChatWebhookPayload:
    cardsV2: list[dict[str, GoogleChatCard]] = field(default_factory=list)

    def add_card(self, card):
        return self.cardsV2.append(dict(cardId="createCardMessage", card=card))


def send_googlechat_webhook(url: str, cards: list[GoogleChatCard]):
    """
    Send Google Chat compatible message as documented in
    https://developers.google.com/chat/messages-overview
    """
    payload = GoogleChatWebhookPayload()
    [payload.add_card(card) for card in cards]
    return requests.post(url, json=asdict(payload), timeout=10)


def send_issue_as_googlechat_webhook(url, issues: list, **kwargs):
    cards = []
    for issue in issues:
        card = GoogleChatCard().construct_issue_card(
            title="GlitchTip Alert",
            issue=issue,
            tags_to_add=kwargs.get("tags_to_add", []),
        )
        cards.append(card)
    return send_googlechat_webhook(url, cards)


def send_webhook_notification(
    notification: "Notification",
    url: str,
    recipient_type: str,
    tags_to_add: list[str] | None = None,
):
    issue_count = notification.issues.count()
    issues = notification.issues.all()[: settings.MAX_ISSUES_PER_ALERT]

    if recipient_type == RecipientType.DISCORD:
        send_issue_as_discord_webhook(url, issues, issue_count, tags_to_add=tags_to_add)
    elif recipient_type == RecipientType.GOOGLE_CHAT:
        send_issue_as_googlechat_webhook(url, issues, tags_to_add=tags_to_add)
    else:
        send_issue_as_webhook(url, issues, issue_count, tags_to_add=tags_to_add)

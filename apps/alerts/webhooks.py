import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import aiohttp
from django.conf import settings

from glitchtip.url_validation import check_url_safe

from .constants import RecipientType

if TYPE_CHECKING:
    from .models import Notification

logger = logging.getLogger(__name__)


async def _is_url_allowed(url: str) -> bool:
    """Runtime SSRF guard for outbound webhook URLs.

    Schema-level validation already rejects private IPs at alert-creation time
    when `GLITCHTIP_ALLOW_PRIVATE_IPS` is False, but this is checked again at
    send time to cover DNS rebinding and config changes between create and fire.
    """
    if await check_url_safe(url, allow_private=settings.GLITCHTIP_ALLOW_PRIVATE_IPS):
        return True
    logger.warning("webhook delivery blocked: URL resolves to a private/internal IP")
    return False


@dataclass
class TagData:
    key: str
    label: str
    value: str


STANDARD_TAG_LABELS = {
    "environment": "Environment",
    "server_name": "Server Name",
    "release": "Release",
}

STANDARD_TAG_KEYS = list(STANDARD_TAG_LABELS.keys())


async def gather_issue_tags(
    issue, tags_to_add: list[str] | None = None
) -> list[TagData]:
    """Fetch standard + custom tags in a single query."""
    all_keys = list(dict.fromkeys(STANDARD_TAG_KEYS + (tags_to_add or [])))
    if not all_keys:
        return []

    results = {
        k: v
        async for k, v in issue.issuetag_set.filter(
            tag_key__key__in=all_keys
        ).values_list("tag_key__key", "tag_value__value")
    }

    tags = []
    for key in all_keys:
        value = results.get(key)
        if value:
            label = STANDARD_TAG_LABELS.get(key, key.capitalize())
            tags.append(TagData(key=key, label=label, value=value))

    return tags


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
class WebhookPayload:
    text: str
    attachments: list[WebhookAttachment]


async def send_webhook(
    url: str,
    message: str,
    attachments: list[WebhookAttachment] | None = None,
):
    if not await _is_url_allowed(url):
        return None
    if not attachments:
        attachments = []
    data = WebhookPayload(text=message, attachments=attachments)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(url, json=asdict(data), timeout=timeout) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_webhook(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    """
    Notification about issues via webhook.
    url: Webhook URL
    issues: This should be only the issues to send as attachment
    issue_count - total issues, may be greater than len(issues)
    """
    attachments: list[WebhookAttachment] = []
    for issue in issues:
        fields = [
            WebhookAttachmentField(
                title="Project",
                value=issue.project.name,
                short=True,
            )
        ]
        tags = await gather_issue_tags(issue, tags_to_add)
        for tag in tags:
            fields.append(
                WebhookAttachmentField(
                    title=tag.label,
                    value=tag.value,
                    short=tag.key in ("environment", "server_name"),
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
    message = "GlitchTip Alert"
    if issue_count > 1:
        message += f" ({issue_count} issues)"
    return await send_webhook(url, message, attachments)


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


async def send_issue_as_discord_webhook(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    embeds: list[DiscordEmbed] = []

    for issue in issues:
        fields = [
            DiscordField(
                name="Project",
                value=issue.project.name,
                inline=True,
            )
        ]
        tags = await gather_issue_tags(issue, tags_to_add)
        for tag in tags:
            fields.append(
                DiscordField(
                    name=tag.label,
                    value=tag.value,
                    inline=tag.key == "environment",
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

    return await send_discord_webhook(url, message, embeds)


async def send_discord_webhook(url: str, message: str, embeds: list[DiscordEmbed]):
    if not await _is_url_allowed(url):
        return None
    payload = DiscordWebhookPayload(content=message, embeds=embeds)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(url, json=asdict(payload), timeout=timeout) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


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
        self, title: str, issue, tags: list[TagData] | None = None
    ):
        self.header = dict(title=title, subtitle=issue.project.name)
        section_header = "<font color='{}'>{}</font>".format(
            issue.get_hex_color(), str(issue)
        )
        widgets = []
        widgets.append(dict(decoratedText=dict(topLabel="Culprit", text=issue.culprit)))
        for tag in tags or []:
            widgets.append(dict(decoratedText=dict(topLabel=tag.label, text=tag.value)))
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


async def send_googlechat_webhook(url: str, cards: list[GoogleChatCard]):
    """
    Send Google Chat compatible message as documented in
    https://developers.google.com/chat/messages-overview
    """
    if not await _is_url_allowed(url):
        return None
    payload = GoogleChatWebhookPayload()
    [payload.add_card(card) for card in cards]
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(url, json=asdict(payload), timeout=timeout) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_googlechat_webhook(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    cards = []
    for issue in issues:
        tags = await gather_issue_tags(issue, tags_to_add)
        card = GoogleChatCard().construct_issue_card(
            title="GlitchTip Alert",
            issue=issue,
            tags=tags,
        )
        cards.append(card)
    return await send_googlechat_webhook(url, cards)


async def send_ntfy(
    url: str,
    title: str,
    message: str,
    click_url: str | None = None,
    tags: list[str] | None = None,
):
    """Send a notification via ntfy (https://ntfy.sh)."""
    if not await _is_url_allowed(url):
        return None
    headers = {
        "Title": title,
        "Markdown": "yes",
    }
    if click_url:
        headers["Click"] = click_url
    if tags:
        headers["Tags"] = ",".join(tags)
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(
                url, data=message.encode("utf-8"), headers=headers, timeout=timeout
            ) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_ntfy(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    title = "GlitchTip Alert"
    if issue_count > 1:
        title += f" ({issue_count} issues)"

    lines = []
    click_url = None
    for issue in issues:
        issue_tags = await gather_issue_tags(issue, tags_to_add)
        lines.append(f"**{issue}**")
        lines.append(f"Project: {issue.project.name}")
        if issue.culprit:
            lines.append(f"Culprit: {issue.culprit}")
        for tag in issue_tags:
            lines.append(f"{tag.label}: {tag.value}")
        lines.append(f"[View Issue {issue.short_id_display}]({issue.get_detail_url()})")
        if click_url is None:
            click_url = issue.get_detail_url()
        lines.append("")

    message = "\n".join(lines).rstrip()
    return await send_ntfy(url, title, message, click_url=click_url, tags=["warning"])


async def send_teams_webhook(
    url: str, card_body: list[dict], actions: list[dict] | None = None
):
    """Send an Adaptive Card to a Microsoft Teams Workflows webhook."""
    if not await _is_url_allowed(url):
        return None
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": card_body,
                    **({"actions": actions} if actions else {}),
                },
            }
        ],
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(url, json=payload, timeout=timeout) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_teams_webhook(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    title = "GlitchTip Alert"
    if issue_count > 1:
        title += f" ({issue_count} issues)"

    body: list[dict] = [
        {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title}
    ]
    actions: list[dict] = []

    for issue in issues:
        tags = await gather_issue_tags(issue, tags_to_add)
        facts = [{"title": "Project", "value": issue.project.name}]
        for tag in tags:
            facts.append({"title": tag.label, "value": tag.value})

        body.append(
            {
                "type": "TextBlock",
                "weight": "Bolder",
                "color": "Attention",
                "text": str(issue),
                "wrap": True,
            }
        )
        if issue.culprit:
            body.append({"type": "TextBlock", "text": issue.culprit, "wrap": True})
        body.append({"type": "FactSet", "facts": facts})
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": f"View Issue {issue.short_id_display}",
                "url": issue.get_detail_url(),
            }
        )

    return await send_teams_webhook(url, body, actions)


async def send_zulip_message(server_url, bot_email, api_key, channel, topic, content):
    """Send a message to a Zulip channel via the native API."""
    if not await _is_url_allowed(server_url):
        return None
    url = f"{server_url.rstrip('/')}/api/v1/messages"
    data = {"type": "channel", "to": channel, "topic": topic, "content": content}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(
                url,
                data=data,
                auth=aiohttp.BasicAuth(bot_email, api_key),
                timeout=timeout,
            ) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_zulip(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    config: dict | None = None,
    **kwargs,
):
    config = config or {}
    title = "GlitchTip Alert"
    if issue_count > 1:
        title += f" ({issue_count} issues)"

    lines = [f"## {title}", ""]
    for issue in issues:
        issue_tags = await gather_issue_tags(issue, tags_to_add)
        lines.append(f"**[{issue}]({issue.get_detail_url()})**")
        lines.append(f"Project: {issue.project.name}")
        if issue.culprit:
            lines.append(f"Culprit: {issue.culprit}")
        for tag in issue_tags:
            lines.append(f"{tag.label}: {tag.value}")
        lines.append("")

    content = "\n".join(lines).rstrip()
    return await send_zulip_message(
        server_url=url,
        bot_email=config.get("bot_email", ""),
        api_key=config.get("api_key", ""),
        channel=config.get("channel", ""),
        topic=config.get("topic", "GlitchTip Alerts"),
        content=content,
    )


async def send_feishu_webhook(url: str, payload: dict) -> None:
    if not await _is_url_allowed(url):
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(**settings.AIOHTTP_CONFIG) as session:
            async with session.post(url, json=payload, timeout=timeout) as resp:
                return resp
    except (TimeoutError, aiohttp.ClientError):
        return None


async def send_issue_as_feishu_webhook(
    url,
    issues: list,
    issue_count: int = 1,
    tags_to_add: list[str] | None = None,
    **kwargs,
):
    title = "GlitchTip Alert"
    if issue_count > 1:
        title += f" ({issue_count} issues)"

    elements = []
    for issue in issues:
        tags = await gather_issue_tags(issue, tags_to_add)
        fields = [
            {
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": f"**Project**\n{issue.project.name}",
                },
            }
        ]
        for tag in tags:
            fields.append(
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{tag.label}**\n{tag.value}",
                    },
                }
            )

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**[{issue}]({issue.get_detail_url()})**\n{issue.culprit or ''}",
                },
                "fields": fields,
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": f"View Issue {issue.short_id_display}",
                        },
                        "url": issue.get_detail_url(),
                        "type": "primary",
                    }
                ],
            }
        )
        elements.append({"tag": "hr"})

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "elements": elements,
        },
    }
    return await send_feishu_webhook(url, payload)


ISSUE_NOTIFICATION_HANDLERS = {
    RecipientType.GENERAL_WEBHOOK: send_issue_as_webhook,
    RecipientType.DISCORD: send_issue_as_discord_webhook,
    RecipientType.FEISHU: send_issue_as_feishu_webhook,
    RecipientType.GOOGLE_CHAT: send_issue_as_googlechat_webhook,
    RecipientType.NTFY: send_issue_as_ntfy,
    RecipientType.MICROSOFT_TEAMS: send_issue_as_teams_webhook,
    RecipientType.ZULIP: send_issue_as_zulip,
}


async def send_test_notification(
    url, recipient_type, project, tags_to_add=None, config=None
):
    """Send a test notification to verify recipient configuration."""
    from apps.issue_events.models import Issue

    issue = await (
        Issue.objects.filter(project=project)
        .select_related("project__organization", "index")
        .order_by("-id")
        .afirst()
    )

    if issue:
        handler = ISSUE_NOTIFICATION_HANDLERS.get(recipient_type, send_issue_as_webhook)
        return await handler(url, [issue], 1, tags_to_add=tags_to_add, config=config)

    # No issues yet — send a basic test via the low-level transport
    title = "GlitchTip Test Notification"
    message = (
        f"Test notification for project {project.name}. "
        "Your alert recipient is configured correctly."
    )

    if recipient_type == RecipientType.ZULIP:
        config = config or {}
        return await send_zulip_message(
            server_url=url,
            bot_email=config.get("bot_email", ""),
            api_key=config.get("api_key", ""),
            channel=config.get("channel", ""),
            topic=config.get("topic", "GlitchTip Alerts"),
            content=f"## {title}\n\n{message}",
        )
    elif recipient_type == RecipientType.NTFY:
        return await send_ntfy(url, title, message, tags=["white_check_mark"])
    elif recipient_type == RecipientType.MICROSOFT_TEAMS:
        card_body = [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": title},
            {"type": "TextBlock", "text": message, "wrap": True},
        ]
        return await send_teams_webhook(url, card_body)
    elif recipient_type == RecipientType.DISCORD:
        embed = DiscordEmbed(
            title=title, description=message, color=0x4B60B4, url="", fields=[]
        )
        return await send_discord_webhook(url, title, [embed])
    elif recipient_type == RecipientType.FEISHU:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "plain_text", "content": message}}
                ],
            },
        }
        return await send_feishu_webhook(url, payload)
    elif recipient_type == RecipientType.GOOGLE_CHAT:
        card = GoogleChatCard()
        card.header = dict(title=title, subtitle=project.name)
        card.sections = [dict(widgets=[dict(decoratedText=dict(text=message))])]
        return await send_googlechat_webhook(url, [card])
    else:
        attachment = WebhookAttachment(title=title, title_link="", text=message)
        return await send_webhook(url, title, [attachment])


async def send_webhook_notification(
    notification: "Notification",
    url: str,
    recipient_type: str,
    tags_to_add: list[str] | None = None,
    config: dict | None = None,
):
    issue_count = await notification.issues.acount()
    issues = [
        issue
        async for issue in notification.issues.select_related(
            "project__organization", "index"
        ).all()[: settings.MAX_ISSUES_PER_ALERT]
    ]

    handler = ISSUE_NOTIFICATION_HANDLERS.get(recipient_type, send_issue_as_webhook)
    await handler(url, issues, issue_count, tags_to_add=tags_to_add, config=config)

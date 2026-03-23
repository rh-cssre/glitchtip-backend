import json
from datetime import datetime
from unittest import mock

import aiohttp
from asgiref.sync import async_to_sync
from model_bakery import baker

from apps.issue_events.constants import LogLevel
from apps.uptime.constants import MonitorType
from apps.uptime.models import Monitor, MonitorCheck
from apps.uptime.webhooks import send_uptime_as_webhook
from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..constants import RecipientType
from ..models import AlertRecipient, Notification
from ..tasks import process_event_alerts
from ..webhooks import (
    send_issue_as_discord_webhook,
    send_issue_as_googlechat_webhook,
    send_issue_as_ntfy,
    send_issue_as_teams_webhook,
    send_issue_as_webhook,
    send_issue_as_zulip,
    send_test_notification,
    send_webhook,
    send_zulip_message,
)

TEST_URL = "https://burkesoftware.rocket.chat/hooks/Y8TttGY7RvN7Qm3gD/rqhHLiRSvYRZ8BhbhhhLYumdMksWnyj3Dqsqt8QKrmbNndXH"
DISCORD_TEST_URL = "https://discord.com/api/webhooks/not_real_id/not_real_token"
GOOGLE_CHAT_TEST_URL = "https://chat.googleapis.com/v1/spaces/space_id/messages?key=api_key&token=api_token"
NTFY_TEST_URL = "https://ntfy.sh/glitchtip-test-topic"
TEAMS_TEST_URL = "https://example.webhook.office.com/webhookb2/test"
ZULIP_TEST_URL = "https://zulip.example.com"
ZULIP_TEST_CONFIG = {
    "bot_email": "bot@zulip.example.com",
    "api_key": "test-api-key",
    "channel": "alerts",
    "topic": "GlitchTip Alerts",
}


def _mock_aiohttp_session():
    """Create a mock aiohttp.ClientSession that captures call args."""
    mock_response = mock.AsyncMock()
    mock_response.status = 200
    # Support async context manager protocol (async with session.post(...) as resp:)
    mock_response.__aenter__ = mock.AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = mock.AsyncMock(return_value=False)

    mock_post = mock.MagicMock(return_value=mock_response)

    mock_session = mock.AsyncMock()
    mock_session.post = mock_post
    mock_session.__aenter__ = mock.AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = mock.AsyncMock(return_value=False)

    mock_constructor = mock.MagicMock(return_value=mock_session)
    return mock_constructor, mock_post


class WebhookTestCase(GlitchTipTestCase):
    def setUp(self):
        self.environment_name = "test-environment"
        self.release_name = "test-release"

        self.create_user_and_project()
        self.monitor = baker.make(
            Monitor,
            name="Example Monitor",
            url="https://example.com",
            monitor_type=MonitorType.GET,
            project=self.project,
        )
        self.monitor_check = baker.make(MonitorCheck, monitor=self.monitor)

        self.expected_subject = "GlitchTip Uptime Alert"
        self.expected_message_down = "The monitored site has gone down."
        self.expected_message_up = "The monitored site is back up."

    def generate_issue_with_tags(self):
        key_environment = baker.make("issue_events.TagKey", key="environment")
        environment_value = baker.make(
            "issue_events.TagValue", value=self.environment_name
        )

        key_release = baker.make("issue_events.TagKey", key="release")
        release_value = baker.make("issue_events.TagValue", value=self.release_name)

        key_custom = baker.make("issue_events.TagKey", key="custom_tag")
        custom_value = baker.make("issue_events.TagValue", value="custom_value")

        issue = baker.make("issue_events.Issue", level=LogLevel.ERROR)
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_environment,
            tag_value=environment_value,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_release,
            tag_value=release_value,
        )
        baker.make(
            "issue_events.IssueTag",
            issue=issue,
            tag_key=key_custom,
            tag_value=custom_value,
        )
        return issue

    @mock.patch("aiohttp.ClientSession")
    def test_send_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_webhook)(
            TEST_URL,
            "from unit test",
        )
        mock_post.assert_called_once()

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        issue2 = baker.make("issue_events.Issue", level=LogLevel.ERROR, short_id=2)
        issue3 = baker.make("issue_events.Issue", level=LogLevel.NOTSET)

        async_to_sync(send_issue_as_webhook)(TEST_URL, [issue, issue2, issue3], 3)

        mock_post.assert_called_once()

        first_issue_json_data = json.dumps(
            mock_post.call_args.kwargs["json"]["attachments"][0]
        )
        self.assertIn(
            f'"title": "Environment", "value": "{self.environment_name}"',
            first_issue_json_data,
        )
        self.assertIn(
            f'"title": "Release", "value": "{self.release_name}"',
            first_issue_json_data,
        )

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_webhook_with_tags_to_add(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_webhook)(
            TEST_URL, [issue], 1, tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"title": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_trigger_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        project = baker.make("projects.Project")
        alert = baker.make(
            "alerts.ProjectAlert",
            project=project,
            timespan_minutes=1,
            quantity=2,
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="example.com",
        )
        issue = baker.make("issue_events.Issue", project=project)

        baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=issue.project.organization,
        )
        process_event_alerts.call()
        self.assertEqual(Notification.objects.count(), 0)

        baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=issue.project.organization,
        )
        process_event_alerts.call()
        self.assertEqual(
            Notification.objects.filter(
                project_alert__alertrecipient__recipient_type=RecipientType.GENERAL_WEBHOOK
            ).count(),
            1,
        )
        mock_post.assert_called_once()
        self.assertIn(
            issue.title, mock_post.call_args[1]["json"]["attachments"][0]["title"]
        )

    @mock.patch("aiohttp.ClientSession")
    def test_trigger_webhook_with_tags_to_add(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        project = baker.make("projects.Project")
        alert = baker.make(
            "alerts.ProjectAlert",
            project=project,
            timespan_minutes=1,
            quantity=2,
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="example.com",
            tags_to_add=["custom_tag"],
        )
        issue = self.generate_issue_with_tags()
        issue.project = project
        issue.save()

        baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=issue.project.organization,
        )
        baker.make(
            "issue_events.IssueEvent",
            issue=issue,
            organization=issue.project.organization,
        )
        process_event_alerts.call()

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"title": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_with_tags_as_discord_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_discord_webhook)(DISCORD_TEST_URL, [issue])

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"name": "Environment", "value": "{self.environment_name}"', json_data
        )
        self.assertIn(f'"name": "Release", "value": "{self.release_name}"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_with_tags_as_discord_webhook_with_tags_to_add(
        self, MockSession
    ):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_discord_webhook)(
            DISCORD_TEST_URL, [issue], 1, tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"name": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_with_tags_as_googlechat_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_googlechat_webhook)(GOOGLE_CHAT_TEST_URL, [issue])

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"topLabel": "Release", "text": "{self.release_name}"', json_data
        )
        self.assertIn(
            f'"topLabel": "Environment", "text": "{self.environment_name}"',
            json_data,
        )

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_with_tags_as_googlechat_webhook_with_tags_to_add(
        self, MockSession
    ):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_googlechat_webhook)(
            GOOGLE_CHAT_TEST_URL, [issue], tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"topLabel": "Custom_tag", "text": "custom_value"', json_data)

    def test_alert_recipient_tags_to_add_default(self):
        alert = baker.make("alerts.ProjectAlert", project=self.project)
        recipient = baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="https://example.com/webhook",
        )
        self.assertEqual(recipient.tags_to_add, [])

    def test_alert_recipient_tags_to_add_custom(self):
        alert = baker.make("alerts.ProjectAlert", project=self.project)
        tags = ["environment", "custom_tag"]
        recipient = baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=RecipientType.GENERAL_WEBHOOK,
            url="https://example.com/webhook",
            tags_to_add=tags,
        )
        self.assertEqual(recipient.tags_to_add, tags)

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_generic_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient, recipient_type=RecipientType.GENERAL_WEBHOOK, url=TEST_URL
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"text": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_down}"', json_data)

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"text": "{self.expected_subject}"', json_data)
        self.assertIn(f'"title": "{self.monitor.name}"', json_data)
        self.assertIn(f'"text": "{self.expected_message_up}"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_google_chat_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient,
            recipient_type=RecipientType.GOOGLE_CHAT,
            url=GOOGLE_CHAT_TEST_URL,
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"title": "{self.expected_subject}", "subtitle": "{self.monitor.name}"',
            json_data,
        )
        self.assertIn(f'"text": "{self.expected_message_down}"', json_data)

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"title": "{self.expected_subject}", "subtitle": "{self.monitor.name}"',
            json_data,
        )
        self.assertIn(f'"text": "{self.expected_message_up}"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_discord_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient, recipient_type=RecipientType.DISCORD, url=DISCORD_TEST_URL
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"content": "{self.expected_subject}"', json_data)
        self.assertIn(
            f'"title": "{self.monitor.name}", "description": "{self.expected_message_down}"',
            json_data,
        )

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(f'"content": "{self.expected_subject}"', json_data)
        self.assertIn(
            f'"title": "{self.monitor.name}", "description": "{self.expected_message_up}"',
            json_data,
        )

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_ntfy(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_ntfy)(NTFY_TEST_URL, [issue])

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["Title"], "GlitchTip Alert")
        self.assertEqual(call_kwargs["headers"]["Markdown"], "yes")
        body = call_kwargs["data"].decode("utf-8")
        self.assertIn(self.environment_name, body)
        self.assertIn(self.release_name, body)
        self.assertIn(issue.project.name, body)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_ntfy_with_tags_to_add(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_ntfy)(
            NTFY_TEST_URL, [issue], 1, tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()
        body = mock_post.call_args.kwargs["data"].decode("utf-8")
        self.assertIn("Custom_tag: custom_value", body)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_ntfy_multiple_issues(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        issue2 = baker.make("issue_events.Issue", level=LogLevel.ERROR, short_id=2)
        async_to_sync(send_issue_as_ntfy)(NTFY_TEST_URL, [issue, issue2], 2)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["Title"], "GlitchTip Alert (2 issues)")

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_ntfy(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient, recipient_type=RecipientType.NTFY, url=NTFY_TEST_URL
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["Title"], self.expected_subject)
        body = call_kwargs["data"].decode("utf-8")
        self.assertIn(self.monitor.name, body)
        self.assertIn(self.expected_message_down, body)

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        body = mock_post.call_args.kwargs["data"].decode("utf-8")
        self.assertIn(self.expected_message_up, body)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_with_issue(self, MockSession):
        """Test notification uses the real issue handler when issues exist."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_test_notification)(
            TEST_URL, RecipientType.GENERAL_WEBHOOK, issue.project
        )
        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(str(issue), json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_with_issue_ntfy(self, MockSession):
        """Test notification uses the ntfy handler when issues exist."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_test_notification)(
            NTFY_TEST_URL, RecipientType.NTFY, issue.project
        )
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["Title"], "GlitchTip Alert")
        body = call_kwargs["data"].decode("utf-8")
        self.assertIn(str(issue), body)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_webhook(self, MockSession):
        """Fallback test notification for generic webhook."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            TEST_URL, RecipientType.GENERAL_WEBHOOK, self.project
        )
        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn("GlitchTip Test Notification", json_data)
        self.assertIn(self.project.name, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_ntfy(self, MockSession):
        """Fallback test notification for ntfy."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            NTFY_TEST_URL, RecipientType.NTFY, self.project
        )
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["Title"], "GlitchTip Test Notification")
        body = call_kwargs["data"].decode("utf-8")
        self.assertIn(self.project.name, body)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_discord(self, MockSession):
        """Fallback test notification for Discord."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            DISCORD_TEST_URL, RecipientType.DISCORD, self.project
        )
        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn("GlitchTip Test Notification", json_data)
        self.assertIn(self.project.name, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_googlechat(self, MockSession):
        """Fallback test notification for Google Chat."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            GOOGLE_CHAT_TEST_URL, RecipientType.GOOGLE_CHAT, self.project
        )
        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn("GlitchTip Test Notification", json_data)
        self.assertIn(self.project.name, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_teams_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_teams_webhook)(TEAMS_TEST_URL, [issue])

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "message")
        card = payload["attachments"][0]["content"]
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.4")

        json_data = json.dumps(payload)
        self.assertIn("GlitchTip Alert", json_data)
        self.assertIn(str(issue), json_data)
        self.assertIn(self.environment_name, json_data)
        self.assertIn(self.release_name, json_data)
        self.assertIn(issue.project.name, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_teams_webhook_with_tags_to_add(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_teams_webhook)(
            TEAMS_TEST_URL, [issue], 1, tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"title": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_teams_webhook_multiple_issues(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        issue2 = baker.make("issue_events.Issue", level=LogLevel.ERROR, short_id=2)
        async_to_sync(send_issue_as_teams_webhook)(TEAMS_TEST_URL, [issue, issue2], 2)

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn("GlitchTip Alert (2 issues)", json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_teams_webhook(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient,
            recipient_type=RecipientType.MICROSOFT_TEAMS,
            url=TEAMS_TEST_URL,
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            True,
            datetime.now(),
        )

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "message")
        json_data = json.dumps(payload)
        self.assertIn(self.expected_subject, json_data)
        self.assertIn(self.monitor.name, json_data)
        self.assertIn(self.expected_message_down, json_data)

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient,
            self.monitor_check.id,
            False,
            datetime.now(),
        )

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(self.expected_message_up, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_with_issue_teams(self, MockSession):
        """Test notification uses the Teams handler when issues exist."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_test_notification)(
            TEAMS_TEST_URL, RecipientType.MICROSOFT_TEAMS, issue.project
        )
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "message")
        json_data = json.dumps(payload)
        self.assertIn(str(issue), json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_teams(self, MockSession):
        """Fallback test notification for Microsoft Teams."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            TEAMS_TEST_URL, RecipientType.MICROSOFT_TEAMS, self.project
        )
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["type"], "message")
        json_data = json.dumps(payload)
        self.assertIn("GlitchTip Test Notification", json_data)
        self.assertIn(self.project.name, json_data)

    @mock.patch("aiohttp.ClientSession")
    def test_send_zulip_message(self, MockSession):
        """Verify Zulip transport sends correct auth and form data."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_zulip_message)(
            ZULIP_TEST_URL,
            "bot@zulip.example.com",
            "test-api-key",
            "alerts",
            "GlitchTip Alerts",
            "Hello from GlitchTip",
        )
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertIsInstance(call_kwargs["auth"], aiohttp.BasicAuth)
        self.assertEqual(call_kwargs["auth"].login, "bot@zulip.example.com")
        self.assertEqual(call_kwargs["auth"].password, "test-api-key")
        self.assertEqual(call_kwargs["data"]["type"], "channel")
        self.assertEqual(call_kwargs["data"]["to"], "alerts")
        self.assertEqual(call_kwargs["data"]["topic"], "GlitchTip Alerts")
        self.assertEqual(call_kwargs["data"]["content"], "Hello from GlitchTip")

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_zulip(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_issue_as_zulip)(
            ZULIP_TEST_URL,
            [issue],
            1,
            tags_to_add=["custom_tag"],
            config=ZULIP_TEST_CONFIG,
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn(str(issue), content)
        self.assertIn(issue.project.name, content)
        self.assertIn(self.environment_name, content)
        self.assertIn(self.release_name, content)
        self.assertIn("Custom_tag: custom_value", content)

    @mock.patch("aiohttp.ClientSession")
    def test_send_issue_as_zulip_multiple_issues(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        issue2 = baker.make("issue_events.Issue", level=LogLevel.ERROR, short_id=2)
        async_to_sync(send_issue_as_zulip)(
            ZULIP_TEST_URL, [issue, issue2], 2, config=ZULIP_TEST_CONFIG
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn("GlitchTip Alert (2 issues)", content)

    @mock.patch("aiohttp.ClientSession")
    def test_send_uptime_events_zulip(self, MockSession):
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        recipient = baker.make(
            AlertRecipient,
            recipient_type=RecipientType.ZULIP,
            url=ZULIP_TEST_URL,
            config=ZULIP_TEST_CONFIG,
        )

        async_to_sync(send_uptime_as_webhook)(
            recipient, self.monitor_check.id, True, datetime.now()
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn(self.monitor.name, content)
        self.assertIn(self.expected_message_down, content)

        mock_post.reset_mock()

        async_to_sync(send_uptime_as_webhook)(
            recipient, self.monitor_check.id, False, datetime.now()
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn(self.expected_message_up, content)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_with_issue_zulip(self, MockSession):
        """Test notification uses the Zulip handler when issues exist."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        issue = self.generate_issue_with_tags()
        async_to_sync(send_test_notification)(
            ZULIP_TEST_URL,
            RecipientType.ZULIP,
            issue.project,
            config=ZULIP_TEST_CONFIG,
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn(str(issue), content)

    @mock.patch("aiohttp.ClientSession")
    def test_send_test_notification_no_issues_zulip(self, MockSession):
        """Fallback test notification for Zulip."""
        mock_constructor, mock_post = _mock_aiohttp_session()
        MockSession.side_effect = mock_constructor

        async_to_sync(send_test_notification)(
            ZULIP_TEST_URL,
            RecipientType.ZULIP,
            self.project,
            config=ZULIP_TEST_CONFIG,
        )
        mock_post.assert_called_once()
        content = mock_post.call_args.kwargs["data"]["content"]
        self.assertIn("GlitchTip Test Notification", content)
        self.assertIn(self.project.name, content)

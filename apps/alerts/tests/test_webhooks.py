import json
from datetime import datetime
from unittest import mock

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
    send_issue_as_webhook,
    send_webhook,
)

TEST_URL = "https://burkesoftware.rocket.chat/hooks/Y8TttGY7RvN7Qm3gD/rqhHLiRSvYRZ8BhbhhhLYumdMksWnyj3Dqsqt8QKrmbNndXH"
DISCORD_TEST_URL = "https://discord.com/api/webhooks/not_real_id/not_real_token"
GOOGLE_CHAT_TEST_URL = "https://chat.googleapis.com/v1/spaces/space_id/messages?key=api_key&token=api_token"


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

    @mock.patch("requests.post")
    def test_send_webhook(self, mock_post):
        send_webhook(
            TEST_URL,
            "from unit test",
        )
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_send_issue_as_webhook(self, mock_post):
        issue = self.generate_issue_with_tags()
        issue2 = baker.make("issue_events.Issue", level=LogLevel.ERROR, short_id=2)
        issue3 = baker.make("issue_events.Issue", level=LogLevel.NOTSET)

        send_issue_as_webhook(TEST_URL, [issue, issue2, issue3], 3)

        mock_post.assert_called_once()

        first_issue_json_data = json.dumps(
            mock_post.call_args.kwargs["json"]["attachments"][0]
        )
        self.assertIn(
            f'"title": "Environment", "value": "{self.environment_name}"',
            first_issue_json_data,
        )
        self.assertIn(
            f'"title": "Release", "value": "{self.release_name}"', first_issue_json_data
        )

    @mock.patch("requests.post")
    def test_send_issue_as_webhook_with_tags_to_add(self, mock_post):
        issue = self.generate_issue_with_tags()
        send_issue_as_webhook(TEST_URL, [issue], 1, tags_to_add=["custom_tag"])

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"title": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("requests.post")
    def test_trigger_webhook(self, mock_post):
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

        baker.make("issue_events.IssueEvent", issue=issue)
        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 0)

        baker.make("issue_events.IssueEvent", issue=issue)
        process_event_alerts()
        self.assertEqual(
            Notification.objects.filter(
                project_alert__alertrecipient__recipient_type=RecipientType.GENERAL_WEBHOOK
            ).count(),
            1,
        )
        mock_post.assert_called_once()
        self.assertIn(
            issue.title, mock_post.call_args[1]["json"]["sections"][0]["activityTitle"]
        )

    @mock.patch("requests.post")
    def test_trigger_webhook_with_tags_to_add(self, mock_post):
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

        baker.make("issue_events.IssueEvent", issue=issue)
        baker.make("issue_events.IssueEvent", issue=issue)
        process_event_alerts()

        mock_post.assert_called_once()
        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"title": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("requests.post")
    def test_send_issue_with_tags_as_discord_webhook(self, mock_post):
        issue = self.generate_issue_with_tags()
        send_issue_as_discord_webhook(DISCORD_TEST_URL, [issue])

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"name": "Environment", "value": "{self.environment_name}"', json_data
        )
        self.assertIn(f'"name": "Release", "value": "{self.release_name}"', json_data)

    @mock.patch("requests.post")
    def test_send_issue_with_tags_as_discord_webhook_with_tags_to_add(self, mock_post):
        issue = self.generate_issue_with_tags()
        send_issue_as_discord_webhook(
            DISCORD_TEST_URL, [issue], 1, tags_to_add=["custom_tag"]
        )

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn('"name": "Custom_tag", "value": "custom_value"', json_data)

    @mock.patch("requests.post")
    def test_send_issue_with_tags_as_googlechat_webhook(self, mock_post):
        issue = self.generate_issue_with_tags()
        send_issue_as_googlechat_webhook(GOOGLE_CHAT_TEST_URL, [issue])

        mock_post.assert_called_once()

        json_data = json.dumps(mock_post.call_args.kwargs["json"])
        self.assertIn(
            f'"topLabel": "Release", "text": "{self.release_name}"', json_data
        )
        self.assertIn(
            f'"topLabel": "Environment", "text": "{self.environment_name}"',
            json_data,
        )

    @mock.patch("requests.post")
    def test_send_issue_with_tags_as_googlechat_webhook_with_tags_to_add(
        self, mock_post
    ):
        issue = self.generate_issue_with_tags()
        send_issue_as_googlechat_webhook(
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

    @mock.patch("requests.post")
    def test_send_uptime_events_generic_webhook(self, mock_post):
        recipient = baker.make(
            AlertRecipient, recipient_type=RecipientType.GENERAL_WEBHOOK, url=TEST_URL
        )

        send_uptime_as_webhook(
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

        send_uptime_as_webhook(
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

    @mock.patch("requests.post")
    def test_send_uptime_events_google_chat_webhook(self, mock_post):
        recipient = baker.make(
            AlertRecipient,
            recipient_type=RecipientType.GOOGLE_CHAT,
            url=GOOGLE_CHAT_TEST_URL,
        )

        send_uptime_as_webhook(
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

        send_uptime_as_webhook(
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

    @mock.patch("requests.post")
    def test_send_uptime_events_discord_webhook(self, mock_post):
        recipient = baker.make(
            AlertRecipient, recipient_type=RecipientType.DISCORD, url=DISCORD_TEST_URL
        )

        send_uptime_as_webhook(
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

        send_uptime_as_webhook(
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

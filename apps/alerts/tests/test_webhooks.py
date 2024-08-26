import json
from unittest import mock

from django.test import TestCase
from model_bakery import baker

from apps.issue_events.constants import LogLevel

from ..constants import RecipientType
from ..models import Notification
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


class WebhookTestCase(TestCase):
    def setUp(self):
        self.environment_name = "test-environment"
        self.release_name = "test-release"

    def generate_issue_with_tags(self):
        key_environment = baker.make("issue_events.TagKey", key="environment")
        environment_value = baker.make(
            "issue_events.TagValue", value=self.environment_name
        )

        key_release = baker.make("issue_events.TagKey", key="release")
        release_value = baker.make("issue_events.TagValue", value=self.release_name)
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

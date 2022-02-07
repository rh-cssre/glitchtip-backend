from unittest import mock
from django.test import TestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from events.models import LogLevel
from ..tasks import process_event_alerts
from ..models import AlertRecipient, Notification
from ..webhooks import send_webhook, send_issue_as_webhook


TEST_URL = "https://burkesoftware.rocket.chat/hooks/Y8TttGY7RvN7Qm3gD/rqhHLiRSvYRZ8BhbhhhLYumdMksWnyj3Dqsqt8QKrmbNndXH"


class WebhookTestCase(TestCase):
    @mock.patch("requests.post")
    def test_send_webhook(self, mock_post):
        send_webhook(
            TEST_URL, "from unit test",
        )
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_send_issue_as_webhook(self, mock_post):
        issue = baker.make("issues.Issue", level=LogLevel.WARNING, short_id=1)
        issue2 = baker.make("issues.Issue", level=LogLevel.ERROR, short_id=2)
        issue3 = baker.make("issues.Issue", level=LogLevel.NOTSET)
        send_issue_as_webhook(TEST_URL, [issue, issue2, issue3], 3)
        mock_post.assert_called_once()

    @mock.patch("requests.post")
    def test_trigger_webhook(self, mock_post):
        project = baker.make("projects.Project")
        alert = baker.make(
            "alerts.ProjectAlert", project=project, timespan_minutes=1, quantity=2,
        )
        baker.make(
            "alerts.AlertRecipient",
            alert=alert,
            recipient_type=AlertRecipient.RecipientType.WEBHOOK,
            url="example.com",
        )
        issue = baker.make("issues.Issue", project=project)

        baker.make("events.Event", issue=issue)
        process_event_alerts()
        self.assertEqual(Notification.objects.count(), 0)

        baker.make("events.Event", issue=issue)
        process_event_alerts()
        self.assertEqual(
            Notification.objects.filter(
                project_alert__alertrecipient__recipient_type=AlertRecipient.RecipientType.WEBHOOK
            ).count(),
            1,
        )
        mock_post.assert_called_once()

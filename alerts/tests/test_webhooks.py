from django.test import TestCase
from model_bakery import baker
from glitchtip import test_utils  # pylint: disable=unused-import
from ..webhooks import send_webhook, send_issue_as_webhook


TEST_URL = "https://burkesoftware.rocket.chat/hooks/Y8TttGY7RvN7Qm3gD/rqhHLiRSvYRZ8BhbhhhLYumdMksWnyj3Dqsqt8QKrmbNndXH"


class WebhookTestCase(TestCase):
    def test_send_webhook(self):
        send_webhook(
            TEST_URL, "from unit test",
        )

    def test_send_issue_as_webhook(self):
        issue = baker.make("issues.Issue")
        send_issue_as_webhook(TEST_URL, issue)
